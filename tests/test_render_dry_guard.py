"""Guard: MCP handler output tables go through the shared renderers.

gr542059 (RENDER_DRY): handler response text must be built with
``precis.format.render_agent_table`` (the ``PRECIS_AGENT_TABLE_FORMAT``
chokepoint), ``precis.utils.table_toon.table_toon``, or
``precis.utils.next_block`` — not hand-rolled ``" | "``-joined or
``\\t``-joined f-string rows. Hand-rolled rows drift apart in style and
can't ride the format swap.

The walk flags, in ``src/precis/handlers/*.py``:

* ``X.join(...)`` calls where *X* is the string constant ``" | "`` or
  ``"\\t"``;
* f-strings carrying a literal ``" | "`` or ``"\\t"`` fragment.

Docstrings (``Expr``-wrapped constants) are skipped. Sites that stay
unconverted live in :data:`_ALLOWLIST` keyed ``(file, qualname)`` with
a reason — shrink it as rows convert; a new hand-rolled row fails here
instead of drifting in silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

_HANDLERS = Path(__file__).resolve().parent.parent / "src" / "precis" / "handlers"

#: Sites left hand-rolled on purpose — ``(file, enclosing def/class
#: name)`` → why. ``<module>`` matches a file-level statement.
_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "_paper_tables.py",
        "_render_table",
    ): "reprints the paper's stored markdown grid — the '|' cells are content, plus a per-row 'ragged' marker with no cell column",
    (
        "cad.py",
        "_connectivity_note",
    ): "one-line warning prose listing disconnected body groups, not record rows",
    (
        "_paper_text.py",
        "_chunk_keywords_or_caption",
    ): "one-line '[table] a | b | …' caption — prose summary, not a row table",
    (
        "todo.py",
        "_check_plan_meta",
    ): "' | ' inside an error next= enumeration of accepted shapes — not output rows",
    (
        "_mode_help.py",
        "require_mode",
    ): "' | ' inside an error options enumeration — not output rows",
    (
        "_measure_render.py",
        "result_line",
    ): "single-line measure row shared with quest/tick prompt lists — needs the multi-caller row-shape refactor (M)",
    (
        "quest.py",
        "_render_measures",
    ): "8-field measure row — needs a column design call before it tables (M)",
    (
        "draft.py",
        "_render_outline",
    ): "depth-indented outline rows ('\\t' size suffix inside the handle cell) — tree+size hybrid, needs row-shape design (M)",
    (
        "draft.py",
        "_row",
    ): "level-map row builder, same '\\t' size-suffix shape as the outline (M)",
    (
        "draft.py",
        "_window_rows",
    ): "level-map window rows, same shape (M)",
    (
        "_finding_edit.py",
        "edit",
    ): "' | ' inside an error-prose mode enumeration — not output rows",
    (
        "_gripe_fix.py",
        "validate_patch",
    ): "' | ' inside an error-prose session-format enumeration — not output rows",
    (
        "cad.py",
        "_render_mold",
    ): "' | ' inside an error-prose layout enumeration — not output rows",
    (
        "_numeric_ref.py",
        "_count_group_line",
    ): "count-view facet rows are the machine-parsed 'label: n' contract (agents + tests read it)",
    (
        "job.py",
        "_count_group_line",
    ): "job's newest/oldest span rides the same count contract",
    (
        "finding.py",
        "_finding_count_view",
    ): "same 'label: n' count contract, finding's copy",
    (
        "paper.py",
        "_count_view",
    ): "same 'label: n' count contract, paper's copy",
    (
        "_python_params.py",
        "render_param",
    ): "single-param hint/default/drift line — k:v pairs on one card, not a row table",
    (
        "_numeric_ref.py",
        "_id_lines",
    ): "view='list' handle+summary+chips line with a '\\t' size suffix — pending conversion to the list table (M)",
}


def _fragments(node: ast.JoinedStr) -> list[str]:
    """Constant string fragments of a JoinedStr (f-string) node."""
    return [
        v.value
        for v in node.values
        if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]


def _scan(path: Path) -> list[tuple[str, int, str]]:
    """``(qualname, lineno, fragment)`` hits for one handler file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int, str]] = []

    def visit(node: ast.AST, qual: str, parent: ast.AST | None) -> None:
        # Docstrings are Expr-wrapped constants — skip the whole node.
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return
        here = qual
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            here = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and isinstance(node.func.value, ast.Constant)
            and node.func.value.value in (" | ", "\t")
        ):
            hits.append((here, node.lineno, f"{node.func.value.value!r}.join(...)"))
        if isinstance(node, ast.JoinedStr):
            for frag in _fragments(node):
                for marker in (" | ", "\t"):
                    if marker in frag:
                        hits.append((here, node.lineno, f"f-string with {marker!r}"))
        for child in ast.iter_child_nodes(node):
            visit(child, here, node)

    visit(tree, "", None)
    return hits


def test_no_hand_rolled_table_rows_in_handlers() -> None:
    violations: list[str] = []
    for path in sorted(_HANDLERS.glob("*.py")):
        for qual, lineno, what in _scan(path):
            if (path.name, qual) in _ALLOWLIST:
                continue
            violations.append(f"{path.name}:{lineno} ({qual or '<module>'}) {what}")
    assert not violations, (
        "hand-rolled table rows in handler output — render via "
        "precis.format.render_agent_table / table_toon instead:\n"
        + "\n".join(violations)
    )


def test_allowlist_sites_exist() -> None:
    """Every allowlist entry still names a live qualname — stale entries
    mean the site was converted or renamed and the list should shrink."""
    found: set[tuple[str, str]] = set()
    for path in sorted(_HANDLERS.glob("*.py")):
        for qual, _lineno, _what in _scan(path):
            if (path.name, qual) in _ALLOWLIST:
                found.add((path.name, qual))
    stale = set(_ALLOWLIST) - found
    assert not stale, f"allowlist entries name no hand-rolled site: {sorted(stale)}"


def test_guard_fires_on_planted_row(tmp_path: Path) -> None:
    """The same scan must flag a planted hand-rolled row."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def x():\n"
        '    a = " | ".join(rows)\n'
        '    b = "\\t".join(cells)\n'
        '    c = f"{x} | {y}"\n'
        '    d = f"{a}\\t{b}"\n',
        encoding="utf-8",
    )
    hits = _scan(planted)
    assert len(hits) == 4, hits
