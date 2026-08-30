"""POSIX-only test shapes must carry the win32 guard — before main reddens.

The Windows CI leg exists only post-merge: the ship gate is a Linux
container and the dev hosts are macOS, so a test that spawns ``bash``,
execs a shebang'd repo shell script (``OSError: [WinError 193]``), or
imports a POSIX-only stdlib module looks green everywhere it runs before
``main`` — and reddens ``check.yml`` for days after. Twice now: 0510182f
guarded the then-existing offenders file-by-file, and the next new module
(``test_reap_stale_serves.py``, five days later) forgot the guard again.
This walk automates the classification so the gate teaches it instead.

A flagged module must contain a ``sys.platform``/"win32" comparison
somewhere — a module ``pytestmark`` skipif (the usual shape, 26 modules of
precedent), a per-test ``skipif``, or an inline runtime skip all count.
WHICH unit to guard stays the author's judgment; THAT the module thought
about Windows does not.

Failure means: add the guard, module-level for a wholly-POSIX module::

    pytestmark = pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX-only <what>"
    )

A pre-merge Windows CI leg was considered and refused: it would hang a
branch push + a ~5 min GitHub runner wait onto every land, against a
local container gate measured in minutes — this static walk catches the
recurring class for free. Genuinely cross-platform false positives go in
``_EXEMPT`` with a reason, not past the guard.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: Modules a human vetted as cross-platform despite tripping a detector.
_EXEMPT: set[str] = set()

#: Stdlib modules that do not exist on Windows — importing one at module
#: scope doesn't just fail a test, it breaks collection of the whole file.
_POSIX_ONLY_IMPORTS = {"fcntl", "grp", "pty", "pwd", "resource", "termios"}

#: Attribute calls that raise/`AttributeError` on Windows.
_POSIX_ONLY_CALLS = {
    ("os", "fork"),
    ("os", "forkpty"),
    ("os", "getpgid"),
    ("os", "killpg"),
    ("os", "mkfifo"),
    ("os", "setsid"),
    ("time", "tzset"),
}

_SHELLS = {"bash", "sh", "zsh", "dash"}
_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}

#: `"scripts" / "reap-stale-serves"` chains and `"scripts/x"` composites —
#: the two idioms tests use to point at repo scripts they then exec.
_PATH_CHAIN = re.compile(r'"(scripts|docker|deploy)"((?:\s*/\s*"[^"]+")+)')
_PATH_LITERAL = re.compile(r'"((?:scripts|docker|deploy)/[^"\n]+)"')


def _test_modules() -> list[Path]:
    return sorted((_ROOT / "tests").rglob("test_*.py"))


def _is_win32_aware(tree: ast.AST) -> bool:
    """Any ``sys.platform`` comparison against a win* constant, anywhere.

    The comparison is the common core of every guard shape — module
    ``pytestmark``, per-test ``skipif`` decorator, inline ``pytest.skip``
    branch — so detecting it covers all three without scope analysis.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and "sys.platform" in ast.unparse(node):
            consts = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant)]
            if any(
                isinstance(v, str) and v.startswith(("win", "cygwin")) for v in consts
            ):
                return True
    return False


def _imported(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(imported module names, names bound via ``from subprocess import``)."""
    modules: set[str] = set()
    from_subprocess: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
            if node.module == "subprocess":
                from_subprocess |= {alias.asname or alias.name for alias in node.names}
    return modules, from_subprocess


def _spawns_shell(tree: ast.AST, from_subprocess: set[str]) -> list[int]:
    """Lines of subprocess calls whose argv[0] is a literal bash/sh."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_spawn = (
            isinstance(func, ast.Attribute)
            and func.attr in _SUBPROCESS_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ) or (
            isinstance(func, ast.Name)
            and func.id in _SUBPROCESS_FUNCS
            and func.id in from_subprocess
        )
        argv = node.args[0]
        if is_spawn and isinstance(argv, ast.List) and argv.elts:
            head = argv.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                if Path(head.value).name in _SHELLS:
                    lines.append(node.lineno)
    return lines


def _posix_only_uses(tree: ast.AST, modules: set[str]) -> list[str]:
    uses = [f"import {m}" for m in sorted(modules & _POSIX_ONLY_IMPORTS)]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in _POSIX_ONLY_CALLS
        ):
            uses.append(f"{node.func.value.id}.{node.func.attr}() line {node.lineno}")
    return uses


def _shell_script_refs(source: str) -> list[str]:
    """Repo-relative paths the module mentions that are shebang'd sh/bash."""
    candidates = set(_PATH_LITERAL.findall(source))
    for root, chain in _PATH_CHAIN.findall(source):
        candidates.add("/".join([root, *re.findall(r'"([^"]+)"', chain)]))
    refs = []
    for rel in sorted(candidates):
        target = _ROOT / rel
        if target.is_file():
            first = target.read_bytes().split(b"\n", 1)[0]
            if first.startswith(b"#!") and b"sh" in first:
                refs.append(rel)
    return refs


def test_posix_only_test_modules_carry_a_win32_guard() -> None:
    bad: list[str] = []
    for path in _test_modules():
        rel = str(path.relative_to(_ROOT))
        if rel in _EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules, from_subprocess = _imported(tree)
        why = [
            f"spawns {s} (line {n})"
            for n in _spawns_shell(tree, from_subprocess)
            for s in ["bash/sh"]
        ]
        why += _posix_only_uses(tree, modules)
        if "subprocess" in modules:
            why += [f"execs shell script {r}" for r in _shell_script_refs(source)]
        if why and not _is_win32_aware(tree):
            bad.append(f"{rel}: {'; '.join(why)}")
    assert not bad, (
        f"{len(bad)} test module(s) look POSIX-only but never consider win32 "
        "(the Windows CI leg only runs post-merge — this is how check.yml "
        "goes red for days). Add a skipif guard, module-level for a wholly-"
        "POSIX module: pytestmark = pytest.mark.skipif(sys.platform == "
        '"win32", reason="POSIX-only <what>") — or vet + _EXEMPT it here:\n'
        + "\n".join(bad)
    )
