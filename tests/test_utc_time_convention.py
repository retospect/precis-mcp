"""Every generated timestamp is UTC — the half nothing else enforces.

The fleet's *runtime* is already pinned: ``deploy/playbooks/00a-timezone.yml``
sets each node's system zone, deploy-rendered units carry ``TZ=UTC``, and
``utils.utc_logging.force_utc_timestamps`` pins ``%(asctime)s`` to
``time.gmtime``. None of that reaches a developer's laptop, where ``date
+%Y%m%d-%H%M%S`` stamps a ``.deploy-logs/`` filename in whatever zone the
machine booted with and labels it as nothing at all. Reading such a filename as
UTC once put a host's clock 45 minutes out until ``date -u`` disproved it.

So this guard covers generation, on any host:

* shell — ``date`` must carry ``-u``/``--utc`` unless it is asking for epoch
  seconds (``date +%s``), which is zone-free by construction;
* Python — no ``date.today()``, no ``datetime.now()`` without a ``tz``, no
  ``datetime.utcnow()`` (naive, and deprecated since 3.12).

Failure means: add ``-u`` to the ``date`` call, or pass ``tz=UTC`` /
``datetime.now(UTC).date()``. The rule and its rationale live in
``docs/conventions/time.md``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ROOTS = ("src", "tests", "scripts", "docker", "deploy")
# Sibling Claude Code worktrees are nested copies of the repo — scanning them
# would redden this test on unrelated in-flight work.
_SKIP = {".claude", ".venv", "node_modules", "build", ".git"}

# A ``date`` invocation: command substitution, backticks, or a bare statement.
# The trailing group captures enough to tell an epoch request from a rendered
# one without parsing shell.
_DATE_CALL = re.compile(r"(?:\$\(|`|^\s*|\|\s*)(date\b[^)`\n|]*)")
# Epoch seconds carry no zone, so they need no ``-u``.
_EPOCH = re.compile(r"^date\s+\+%s\s*$")


def _shell_sources() -> list[Path]:
    out: list[Path] = []
    for root in _ROOTS:
        for p in (_ROOT / root).rglob("*"):
            if not p.is_file() or _SKIP & set(p.relative_to(_ROOT).parts):
                continue
            if p.suffix in (".sh", ".j2", ".bash", ""):
                out.append(p)
    return out


def _python_sources() -> list[Path]:
    return [
        p
        for root in _ROOTS
        for p in (_ROOT / root).rglob("*.py")
        if not _SKIP & set(p.relative_to(_ROOT).parts)
    ]


def _shell_offenders(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for call in _DATE_CALL.findall(line):
            call = call.strip()
            flags = call.split()
            if "-u" in flags or "--utc" in flags:
                continue
            if _EPOCH.match(call):
                continue
            out.append((lineno, call))
    return out


def _python_offenders(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        recv = ast.unparse(node.func.value)
        tail = recv.split(".")[-1]
        if attr == "utcnow":
            out.append((node.lineno, f"{recv}.utcnow()"))
        elif attr == "today" and tail in ("date", "datetime"):
            out.append((node.lineno, f"{recv}.today()"))
        elif attr == "now" and tail in ("datetime", "dt"):
            if not node.args and not any(kw.arg == "tz" for kw in node.keywords):
                out.append((node.lineno, f"{recv}.now()"))
    return out


def test_shell_timestamps_are_utc() -> None:
    bad: list[str] = []
    for path in _shell_sources():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary artefact under one of the scanned roots
        if "date" not in text:
            continue
        rel = path.relative_to(_ROOT)
        bad += [f"{rel}:{line} {call}" for line, call in _shell_offenders(text)]
    assert not bad, (
        f"{len(bad)} shell `date` call(s) render in the host's local zone — "
        "add -u (see docs/conventions/time.md):\n" + "\n".join(sorted(bad))
    )


def test_python_timestamps_are_utc() -> None:
    bad: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_bytes())
        rel = path.relative_to(_ROOT)
        bad += [f"{rel}:{line} {what}" for line, what in _python_offenders(tree)]
    assert not bad, (
        f"{len(bad)} timezone-naive clock read(s) — pass tz=UTC "
        "(see docs/conventions/time.md):\n" + "\n".join(sorted(bad))
    )
