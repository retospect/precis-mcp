"""gr512233 slice 2: hand-built ``Next:`` trailers are pinned until slice 6.

``Next:`` rows must flow through ``utils/next_block.py::render_next_section``
so the read-scope filter (``is_write_call``) reaches them. Sites that
string-concat a ``Next:`` trailer by hand bypass that filter; slice 6
moves them onto the renderer. Until then this ratchet pins the current
file list so the set cannot grow.

A file counts when a *non-docstring* string constant in its AST contains
``Next:`` (comments and docstrings are documentation, not emitted
trailers; the sanctioned renderer ``utils/next_block.py`` is pinned
too — the baseline records state, not approval). The baseline is
``tests/data/next_literal_baseline.json``, a one-way floor keyed
``file -> count``:

* a new file or a raised count fails: route the trailer through
  ``render_next_section`` instead;
* a dropped count or a file leaving the list fails until the baseline
  is lowered — it only ever goes down (slice 6's acceptance is the
  empty map).

Regenerate only downwards: ``uv run python tests/test_next_literal_ratchet.py
--write`` refuses to add or raise any entry.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "precis"
BASELINE = Path(__file__).resolve().parent / "data" / "next_literal_baseline.json"


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids of string constants used as docstrings (module/class/def)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def count_next_literals(path: Path) -> int:
    """Non-docstring string constants containing ``Next:`` in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_ids = _docstring_node_ids(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "Next:" in node.value
        and id(node) not in doc_ids
    )


def current() -> dict[str, int]:
    """``{repo-relative file: emitted ``Next:`` count}`` for src/precis."""
    out: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        n = count_next_literals(path)
        if n:
            out[str(path.relative_to(ROOT))] = n
    return out


def test_hand_built_next_list_only_shrinks() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    now = current()
    failures: list[str] = []
    for file, n in sorted(now.items()):
        base = baseline.get(file)
        if base is None:
            failures.append(
                f"{file}: new hand-built Next: trailer site — route it "
                "through utils/next_block.py::render_next_section so the "
                "read-scope filter reaches it"
            )
        elif n > base:
            failures.append(
                f"{file}: hand-built Next: count rose {base} -> {n} — "
                "route the new trailer through render_next_section"
            )
    for file, base in sorted(baseline.items()):
        gone = now.get(file)
        if gone is None:
            failures.append(
                f"{file}: left the baseline — regenerate with --write "
                "(the list only goes down)"
            )
        elif gone < base:
            failures.append(
                f"{file}: count fell {base} -> {gone} — lower the baseline with --write"
            )
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    if "--write" in sys.argv:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        now = current()
        lowered = {f: n for f, n in now.items() if f in baseline and n < baseline[f]}
        raised = {f: n for f, n in now.items() if n > baseline.get(f, 0)}
        if raised:
            sys.exit(
                "refusing to raise/add baseline entries: "
                + ", ".join(sorted(raised))
                + " — route those trailers through render_next_section"
            )
        merged = {**baseline, **lowered}
        merged = {f: n for f, n in merged.items() if f in now}
        BASELINE.write_text(
            json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline lowered: {len(lowered)} counts, {len(merged)} files remain")
    else:
        print(json.dumps(current(), indent=2, sort_keys=True))
