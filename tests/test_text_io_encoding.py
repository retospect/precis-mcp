"""Text IO must name its encoding — the half ruff cannot enforce.

``open()``/``Path.open()``/``read_text()``/``write_text()`` default to the
*locale* encoding. That is UTF-8 on every machine this project runs on in
anger, but cp1252 on the Windows CI leg, where a bare read of a UTF-8 file
raises ``UnicodeDecodeError`` and nothing reproduces locally. Ruff ``PLW1514``
covers this — but only partway:

    p = Path(d, "x"); p.read_text()      # flagged
    q = Path(d) / "x"; q.read_text()     # NOT flagged

``PLW1514``'s inference tracks a name as a ``Path`` only while it is bound
directly to a ``Path(...)`` call; the ``/`` binop erases the binding, and that
is the dominant path-building idiom here. Ruff caught 69 sites; the real
population was 388. So the lint rule guards new code written the first way and
this test guards the rest — neither is redundant.

Failure means: add ``encoding="utf-8"``. If a site genuinely needs bytes, open
it in binary mode instead; if it genuinely needs the locale encoding, name it
explicitly so the intent is legible.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ROOTS = ("src", "tests", "scripts", "docker", "deploy")
# Sibling Claude Code worktrees are nested copies of the repo — scanning them
# would redden this test on unrelated in-flight work (same reason
# ``[tool.ruff].extend-exclude`` skips them).
_SKIP = {".claude", ".venv", "node_modules", "build", ".git"}
# These method names are not pathlib-exclusive. ``Tag.open`` is a domain tag
# constructor; fitz/tarfile/zipfile/gzip/Image/os are binary or take no
# ``encoding``; ``importlib.metadata.distribution(...).read_text(filename)``
# has no ``encoding`` parameter at all (passing one is a TypeError, which mypy
# catches — this list keeps the guard from demanding it in the first place).
_NOT_PATHLIB = {
    "Tag",
    "fitz",
    "tarfile",
    "Image",
    "os",
    "zipfile",
    "gzip",
    "cls",
    "pool",
    "distribution",
    "opener",  # urllib OpenerDirector.open(request, timeout=...)
}


def _sources() -> list[Path]:
    return [
        p
        for root in _ROOTS
        for p in (_ROOT / root).rglob("*.py")
        if not _SKIP & set(p.relative_to(_ROOT).parts)
    ]


def _mode(call: ast.Call, *, arg_index: int) -> str:
    """Mode string, or "" when it isn't a literal.

    ``arg_index`` differs by call form: the builtin is ``open(file, mode)`` so
    mode is positional 1, while ``Path.open(mode)`` carries the receiver
    separately and mode is positional 0. Reading the wrong slot makes every
    ``open(p, "rb")`` look like an un-encoded text read.
    """
    if len(call.args) > arg_index:
        arg = call.args[arg_index]
        if isinstance(arg, ast.Constant):
            return str(arg.value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return ""


def _offenders(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            if "b" not in _mode(node, arg_index=1):
                out.append((node.lineno, "open()"))
        elif isinstance(func, ast.Attribute):
            recv = ast.unparse(func.value).split("(")[0].strip()
            if recv in _NOT_PATHLIB:
                continue
            if func.attr in ("read_text", "write_text"):
                out.append((node.lineno, f".{func.attr}()"))
            elif func.attr == "open" and "b" not in _mode(node, arg_index=0):
                out.append((node.lineno, ".open()"))
    return out


def test_text_io_names_its_encoding() -> None:
    bad: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_bytes())
        rel = path.relative_to(_ROOT)
        bad += [f"{rel}:{line} {what}" for line, what in _offenders(tree)]
    assert not bad, (
        f"{len(bad)} text-IO call(s) rely on the locale encoding "
        f'(UnicodeDecodeError on the Windows CI leg) — add encoding="utf-8":\n'
        + "\n".join(sorted(bad))
    )
