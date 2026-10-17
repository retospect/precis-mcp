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

Approved exemption: a raise whose first line (trailing comment) or the line
directly above carries ``# no-next: <category> — <reason>`` is conforming-
exempt. Categories are a closed set (``terminal``: no caller action changes
the outcome; ``internal``: server bug / broken invariant) and the reason is
at least 4 words. Exempt raises are counted per category and pinned as a
CEILING in ``tests/data/mcp_raise_next_exempt.json``: adding a pragma fails
until that JSON is bumped in the same diff (a reviewed change); removing one
fails until the ceiling is lowered. Malformed markers fail the test.

Regenerate only downwards: ``uv run python tests/test_mcp_raise_next_ratchet.py
--write`` refuses to raise or add any entry.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "precis"
BASELINE = Path(__file__).resolve().parent / "data" / "mcp_raise_next_baseline.json"
EXEMPT = Path(__file__).resolve().parent / "data" / "mcp_raise_next_exempt.json"

CATEGORIES = ("terminal", "internal")
MIN_REASON_WORDS = 4
_MARKER = re.compile(r"#\s*no-next:\s*(.*)$")
_PARTS = re.compile(r"^(\S+)\s*(?:—|--?)\s*(.*)$")

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


def parse_marker(comment: str) -> tuple[str, str] | None:
    """Parse a ``# no-next: <category> — <reason>`` comment.

    Returns ``(category, "")`` on success, ``("", error)`` on a malformed
    marker, ``None`` when ``comment`` holds no marker.
    """
    m = _MARKER.search(comment)
    if not m:
        return None
    parts = _PARTS.match(m.group(1).strip())
    if not parts:
        return ("", "expected '# no-next: <category> — <reason>'")
    cat, reason = parts.group(1), parts.group(2).strip()
    if cat not in CATEGORIES:
        return ("", f"unknown category {cat!r} (allowed: {', '.join(CATEGORIES)})")
    if len(reason.split()) < MIN_REASON_WORDS:
        return ("", f"reason needs >= {MIN_REASON_WORDS} words, got {reason!r}")
    return (cat, "")


def find_marker(lines: list[str], lineno: int) -> tuple[str, str] | None:
    """Marker on the raise's first line (1-based) or a comment-only line above."""
    if 1 <= lineno <= len(lines):
        hit = parse_marker(lines[lineno - 1])
        if hit:
            return hit
    if 2 <= lineno <= len(lines) + 1:
        above = lines[lineno - 2]
        if above.lstrip().startswith("#"):
            return parse_marker(above)
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, lines: list[str] | None = None) -> None:
        self.rel = rel
        self.lines = lines or []
        self.exempt: Counter[str] = Counter()
        self.malformed: list[str] = []
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
            marker = find_marker(self.lines, node.lineno) if name in TYPED else None
            if marker and marker[1]:
                self.malformed.append(f"{self.rel}:{node.lineno}: {marker[1]}")
            elif marker:
                self.exempt[marker[0]] += 1
            elif name in TYPED and not _conforms(exc):
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


def _scan_all() -> tuple[dict[str, int], dict[str, int], list[str]]:
    out: Counter[str] = Counter()
    exempt: Counter[str] = Counter()
    malformed: list[str] = []
    for f in _files():
        text = f.read_text(encoding="utf-8")
        v = _Visitor(str(f.relative_to(SRC)), text.splitlines())
        v.visit(ast.parse(text))
        out.update(v.bad)
        exempt.update(v.exempt)
        malformed += v.malformed
    return dict(sorted(out.items())), dict(sorted(exempt.items())), malformed


def scan() -> dict[str, int]:
    return _scan_all()[0]


def _exempt_ceiling() -> dict[str, int]:
    return json.loads(EXEMPT.read_text(encoding="utf-8"))


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


def test_no_malformed_no_next_markers() -> None:
    malformed = _scan_all()[2]
    assert not malformed, f"malformed '# no-next:' marker(s): {malformed}"


def test_exemptions_match_pinned_ceiling() -> None:
    ceiling = _exempt_ceiling()
    now = _scan_all()[1]
    cats = sorted(set(ceiling) | set(now))
    up = {
        c: (ceiling.get(c, 0), now.get(c, 0))
        for c in cats
        if now.get(c, 0) > ceiling.get(c, 0)
    }
    down = {
        c: (ceiling.get(c, 0), now.get(c, 0))
        for c in cats
        if now.get(c, 0) < ceiling.get(c, 0)
    }
    assert not up, (
        f"new '# no-next:' exemption(s) (pinned -> now): {up}. Bump "
        "tests/data/mcp_raise_next_exempt.json in the same diff so the "
        "exemption is a reviewed change, and list it in the commit body."
    )
    assert not down, (
        f"exemptions removed (pinned -> now): {down}. Lower the ceiling in "
        "tests/data/mcp_raise_next_exempt.json."
    )


def test_exempt_ceiling_categories_are_closed() -> None:
    assert set(_exempt_ceiling()) <= set(CATEGORIES)


def test_parse_marker_good() -> None:
    good = "raise X()  # no-next: terminal — clock read cannot be fixed"
    assert parse_marker(good) == ("terminal", "")
    dashes = parse_marker("# no-next: internal -- broken server invariant here")
    assert dashes == ("internal", "")


def test_parse_marker_none() -> None:
    assert parse_marker("raise X()  # something else") is None


def test_parse_marker_unknown_category() -> None:
    hit = parse_marker("# no-next: whatever — has a long enough reason")
    assert hit is not None and hit[0] == "" and "unknown category" in hit[1]


def test_parse_marker_short_reason() -> None:
    hit = parse_marker("# no-next: terminal — too short")
    assert hit is not None and hit[0] == "" and "words" in hit[1]


def test_find_marker_line_above_and_trailing() -> None:
    lines = [
        "def f():",
        "    # no-next: terminal — nothing the caller can change",
        "    raise Internal('x')",
        "    raise Internal('y')  # no-next: internal — broken invariant in server",
        "    raise Internal('z')",
    ]
    assert find_marker(lines, 3) == ("terminal", "")
    assert find_marker(lines, 4) == ("internal", "")
    assert find_marker(lines, 5) is None  # line above is code, not a comment


def test_visitor_exempts_marked_raise() -> None:
    src = (
        "def f():\n"
        "    # no-next: terminal — nothing the caller can change\n"
        "    raise Internal('x')\n"
        "    raise Internal('y')\n"
    )
    v = _Visitor("h.py", src.splitlines())
    v.visit(ast.parse(src))
    assert dict(v.exempt) == {"terminal": 1}
    assert dict(v.bad) == {"h.py::f::Internal": 1}


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
