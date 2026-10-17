"""Enforcers for docs/conventions/code-anchors.md and llm-facing-prose.md.

- ``code-anchors/cite-by-symbol``: zero bare ``file.py:NNN`` refs in
  ``docs/`` outside ``docs/backlog/``; ratchet on written ``path.py::Sym`` anchors that no longer resolve
  (``scripts/coderef check docs``).
- ``code-anchors/ast-resolution-not-index``: ``scripts/coderef`` imports only
  stdlib + grimp (no precis package, no MCP/network client).
- ``llm-facing-prose/update-doc-same-commit``: zero embedded commit shas in
  ``docs/`` outside ``docs/backlog/`` (a gate value takes ``sha-gate: allow``) (verified-at date stamps are too heuristic to gate).

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

# ``scripts/coderef check docs`` drifted anchors, excluding the known-absent
# ``hexfold/*`` paths (present in some checkouts only). Must be exactly 0.
_DRIFT_EXPECTED = 0
# coderef may use stdlib + grimp (its import-graph verbs); never the product
# package, an MCP client or a network library.
_FORBIDDEN_ROOTS = {"precis", "precis_web", "precis_se", "mcp", "httpx", "requests"}


_ALLOW = "sha-gate: allow"


def _unmarked(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if _ALLOW not in ln)


def bare_line_refs(text: str) -> list[str]:
    return [m.group(0) for m in _BARE_LINE.finditer(_unmarked(text))]


def sha_refs(text: str) -> list[str]:
    return [m.group(0) for m in _SHA.finditer(_unmarked(text))]


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


#: Hook-generated, git-ignored views; absent in CI, so counting them makes
#: a local run disagree with the gate.
_GENERATED = {
    "backlog/INDEX.md",
    "backlog/threads/PRIORITIES.md",
    "codebase-map.md",
    "runbooks/INDEX.md",
}


def _durable_docs() -> list[Path]:
    """docs/ minus docs/backlog/ (dated work-in-progress evidence) and the
    generated views."""
    return [
        p
        for p in sorted(DOCS.rglob("*.md"))
        if p.relative_to(DOCS).parts[0] != "backlog"
        and p.relative_to(DOCS).as_posix() not in _GENERATED
    ]


def _count(fn, files: list[Path]) -> int:
    return sum(len(fn(p.read_text(encoding="utf-8"))) for p in files)


def test_no_bare_line_refs_in_durable_docs() -> None:
    n = _count(bare_line_refs, _durable_docs())
    assert n == 0, (
        f"{n} bare file.py:NNN refs in docs/ outside backlog/; "
        "cite path.py::Qual.name (scripts/coderef anchor)"
    )


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


def test_no_commit_shas_in_durable_docs() -> None:
    n = _count(sha_refs, _durable_docs())
    assert n == 0, (
        f"{n} commit shas in docs/ outside backlog/; freshness is the doc's "
        f"git history. A gate value that must name a sha takes '{_ALLOW}'."
    )


def test_checkers_fire_on_synthetic_violations() -> None:
    assert bare_line_refs("see src/precis/dispatch.py:174 and review.py:308")
    assert not bare_line_refs("see src/precis/dispatch.py::Hub.register_ability")
    assert not sha_refs("commit 252e40f49 <!-- sha-gate: allow — gate value -->")
    assert sha_refs("landed in commit 252e40f49 yesterday")
    assert sha_refs("rechecked at 5c692aad3")
    assert not sha_refs("see git log for history")
    assert forbidden_imports("import precis.mcp\nfrom httpx import get")
    assert not forbidden_imports("import ast\nimport grimp")
