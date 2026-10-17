"""Enforcers for docs/conventions/code-anchors.md and llm-facing-prose.md.

- ``code-anchors/cite-by-symbol``: ratchet on bare ``file.py:NNN`` refs in
  ``docs/``; ratchet on written ``path.py::Sym`` anchors that no longer resolve
  (``scripts/coderef check docs``).
- ``code-anchors/ast-resolution-not-index``: ``scripts/coderef`` imports only
  stdlib + grimp (no precis package, no MCP/network client).
- ``llm-facing-prose/update-doc-same-commit``: ratchet on embedded commit shas
  in ``docs/`` (verified-at date stamps are too heuristic to gate).

Judgement-only, no static test: ``code-anchors/citation-surfaces-only`` (the
tree has ~86 ``path.py::Sym`` pointers in src comments by practice; the
convention only says do not nag them), ``llm-facing-prose/terse-no-warmup``,
``dont-justify-choices``, ``name-internals-in-dev-docs``, ``point-dont-copy``,
``present-tense-invariants``.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

_BARE_LINE = re.compile(
    r"(?<![\w/.])((?:src|tests|scripts|deploy)/[\w./-]+\.py|\b\w+\.py):(\d+)\b"
)
_SHA = re.compile(
    r"\b(?:commit|sha|SHA|at)\s+`?(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{9,40}\b"
)

# Frozen on 2026-10-17: bare ``file.py:NNN`` refs under docs/ (85 backlog,
# 2 conventions). May only shrink.
_BARE_LINE_MAX = 87
# ``scripts/coderef check docs`` drifted anchors, excluding the known-absent
# ``hexfold/*`` paths (present in some checkouts only). Must be exactly 0.
_DRIFT_EXPECTED = 0
# Frozen on 2026-10-17: commit shas in docs/ (12 in backlog dated evidence, 1 in
# a runbook gate value). May only shrink.
_SHA_MAX = 13

# coderef may use stdlib + grimp (its import-graph verbs); never the product
# package, an MCP client or a network library.
_FORBIDDEN_ROOTS = {"precis", "precis_web", "precis_se", "mcp", "httpx", "requests"}


def bare_line_refs(text: str) -> list[str]:
    return [m.group(0) for m in _BARE_LINE.finditer(text)]


def sha_refs(text: str) -> list[str]:
    return [m.group(0) for m in _SHA.finditer(text)]


def forbidden_imports(source: str) -> list[str]:
    bad = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [node.module or ""]
        else:
            continue
        bad += [m for m in mods if m.split(".")[0] in _FORBIDDEN_ROOTS]
    return bad


def _docs(*, backlog: bool) -> list[Path]:
    return [
        p
        for p in sorted(DOCS.rglob("*.md"))
        if (p.relative_to(DOCS).parts[0] == "backlog") == backlog
    ]


def _count(fn, files: list[Path]) -> int:
    return sum(len(fn(p.read_text(encoding="utf-8"))) for p in files)


def test_bare_line_refs_ratchet() -> None:
    n = _count(bare_line_refs, _docs(backlog=True) + _docs(backlog=False))
    assert n <= _BARE_LINE_MAX, (
        f"{n} bare file.py:NNN refs in docs/ (measured {_BARE_LINE_MAX} on "
        "2026-10-17); cite path.py::Qual.name (scripts/coderef anchor)"
    )
    assert n == _BARE_LINE_MAX, f"fixed some; lower _BARE_LINE_MAX to {n}"


def test_coderef_drift_ratchet() -> None:
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "coderef"), "check", "docs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert r.returncode == 0, f"coderef crashed ({r.returncode}):\n{r.stderr}"
    assert re.search(
        r"^coderef: (?:\d+ drifted|\S+ no anchor drift)", r.stdout, re.M
    ), f"coderef printed no summary line:\n{r.stdout}\n{r.stderr}"
    drifted = [
        ln.strip()
        for ln in r.stdout.splitlines()
        if ln.strip().startswith("\u2717") and "hexfold/" not in ln
    ]
    assert len(drifted) == _DRIFT_EXPECTED, (
        f"{len(drifted)} drifted anchors (expected {_DRIFT_EXPECTED}, hexfold/* "
        "excluded):\n" + "\n".join(drifted)
    )


def test_coderef_is_stdlib_ast_only() -> None:
    src = (ROOT / "scripts" / "coderef").read_text(encoding="utf-8")
    assert not forbidden_imports(src), forbidden_imports(src)
    assert "import ast" in src


def test_sha_ratchet() -> None:
    n = _count(sha_refs, _docs(backlog=True) + _docs(backlog=False))
    assert n <= _SHA_MAX, (
        f"{n} commit shas in docs/ (measured {_SHA_MAX} on 2026-10-17); "
        "freshness is the doc's history, not an embedded sha"
    )
    assert n == _SHA_MAX, f"fixed some; lower _SHA_MAX to {n}"


def test_checkers_fire_on_synthetic_violations() -> None:
    assert bare_line_refs("see src/precis/dispatch.py:174 and review.py:308")
    assert not bare_line_refs("see src/precis/dispatch.py::Hub.register_ability")
    assert sha_refs("landed in commit 252e40f49 yesterday")
    assert sha_refs("rechecked at 5c692aad3")
    assert not sha_refs("see git log for history")
    assert forbidden_imports("import precis.mcp\nfrom httpx import get")
    assert not forbidden_imports("import ast\nimport grimp")
