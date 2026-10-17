"""M3 (gr494831): handler raises must carry ``next=`` (or ``options=``).

Rule (docs/conventions/mcp-authoring.md): a refusal names the fix. This
walks every ``raise <PrecisError>(...)`` in ``src/precis/handlers/*.py``
and ``diagram/handler.py`` and classifies it:

* conforming: a keyword ``next=`` or ``options=``, or a message literal
  that itself points at ``get(kind='skill'``;
* non-conforming: anything else (typed error, no hint).

Existing non-conforming sites are pinned in
``tests/data/mcp_raise_next_baseline.json`` as a ONE-WAY floor keyed
``file::enclosing function::ExceptionClass`` -> count (not line numbers, so
unrelated edits do not churn it). The test fails when

* a key's count rises or a new key appears: add ``next=`` to the raise;
* a count falls: lower the baseline (it only ever goes down).

Regenerate only downwards: ``uv run python tests/test_mcp_raise_next_ratchet.py
--write`` refuses to raise or add any entry.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "precis"
BASELINE = Path(__file__).resolve().parent / "data" / "mcp_raise_next_baseline.json"

TYPED = {
    "BadInput",
    "NotFound",
    "Gone",
    "Unsupported",
    "Denied",
    "Upstream",
    "RateLimited",
    "Internal",
    "PrecisError",
    "Unavailable",
}


def _files() -> list[Path]:
    files = sorted((SRC / "handlers").glob("*.py"))
    extra = SRC / "diagram" / "handler.py"
    if extra.exists():
        files.append(extra)
    return files


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.bad: Counter[str] = Counter()

    def _scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope(node, node.name)

    def visit_Raise(self, node: ast.Raise) -> None:
        exc = node.exc
        if isinstance(exc, ast.Call):
            fn = exc.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name in TYPED and not _conforms(exc):
                self.bad[
                    f"{self.rel}::{'.'.join(self.stack) or '<module>'}::{name}"
                ] += 1
        self.generic_visit(node)


def _conforms(call: ast.Call) -> bool:
    if any(k.arg in ("next", "options") for k in call.keywords):
        return True
    # a message literal that already points at a skill
    for sub in ast.walk(call):
        if (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and "get(kind='skill'" in sub.value
        ):
            return True
    return False


def scan() -> dict[str, int]:
    out: Counter[str] = Counter()
    for f in _files():
        v = _Visitor(str(f.relative_to(SRC)))
        v.visit(ast.parse(f.read_text(encoding="utf-8")))
        out.update(v.bad)
    return dict(sorted(out.items()))


def _baseline() -> dict[str, int]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_no_new_hintless_handler_raises() -> None:
    base = _baseline()
    now = scan()
    worse = {k: (base.get(k, 0), n) for k, n in now.items() if n > base.get(k, 0)}
    assert not worse, (
        "handler raise(s) without next=/options= (pinned -> now): "
        f"{worse}. Add next='<copy-pasteable call that fixes it>' or "
        "options=[...] to the raise (docs/conventions/mcp-authoring.md)."
    )


def test_baseline_only_goes_down() -> None:
    base = _baseline()
    now = scan()
    stale = {k: (n, now.get(k, 0)) for k, n in base.items() if now.get(k, 0) < n}
    assert not stale, (
        "baseline is higher than the tree (pinned -> now): "
        f"{stale}. Lower it: uv run python "
        "tests/test_mcp_raise_next_ratchet.py --write"
    )


def _write() -> None:
    now = scan()
    if BASELINE.exists():
        base = _baseline()
        up = {k: n for k, n in now.items() if n > base.get(k, 0)}
        if up:
            raise SystemExit(f"refusing to raise the floor: {up}")
    BASELINE.parent.mkdir(exist_ok=True)
    BASELINE.write_text(json.dumps(now, indent=1) + "\n", encoding="utf-8")
    print(f"pinned {sum(now.values())} sites in {len(now)} keys")


if __name__ == "__main__":
    if "--write" in sys.argv:
        _write()
    else:
        print(sum(scan().values()))
