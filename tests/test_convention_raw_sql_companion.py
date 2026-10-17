"""Enforcer for testing/raw-sql-needs-real-pg-test (docs/conventions/testing.md).

Every ``precis_web`` module with raw SQL needs a
``tests/precis_web/test_<module>_sql.py`` companion that runs the SQL on real
Postgres (FakeStore never parses SQL). One-way ratchet: the modules in
``_GRANDFATHERED`` pre-date the rule (18 measured 2026-10-17 of 26 with raw
SQL); a NEW module with SQL and no companion fails. Adding the companion
means deleting the entry here (``test_grandfathered_entries_are_still_missing``
forces it).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "src" / "precis_web"
_TESTS = _ROOT / "tests" / "precis_web"

_SQL = re.compile(
    r"^\s*(SELECT\b.*\bFROM\b|INSERT\s+INTO\b|UPDATE\s+\w+.*\bSET\b"
    r"|DELETE\s+FROM\b|WITH\s+\w+\s+AS\b)",
    re.S | re.I,
)

_GRANDFATHERED = frozenset(
    {
        "claim_render", "design_turn", "env_context", "nav", "pcb_design",
        "quest_board", "agentlogs", "blocktree_view", "cad", "categorizers",
        "clusters", "design", "drafts", "factory", "items", "papers",
        "preview", "refs",
    }
)  # fmt: skip


def has_raw_sql(source: str) -> bool:
    """True when a non-docstring string literal looks like a SQL statement."""
    tree = ast.parse(source)
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(
            n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docs
        and _SQL.match(n.value)
        for n in ast.walk(tree)
    )


def _sql_modules() -> set[str]:
    return {
        p.stem for p in _WEB.rglob("*.py") if has_raw_sql(p.read_text(encoding="utf-8"))
    }


def _missing_companions() -> set[str]:
    return {m for m in _sql_modules() if not (_TESTS / f"test_{m}_sql.py").exists()}


def test_checker_flags_synthetic_sql() -> None:
    assert has_raw_sql('def f(c):\n    c.execute("SELECT id FROM refs WHERE x = %s")\n')
    assert has_raw_sql('Q = """\\n  UPDATE refs SET a = 1\\n"""\n')
    assert not has_raw_sql('def f():\n    """SELECT things FROM somewhere."""\n')
    assert not has_raw_sql('X = "select a menu item"\n')


def test_new_raw_sql_modules_have_a_real_pg_companion() -> None:
    new = sorted(_missing_companions() - _GRANDFATHERED)
    assert not new, (
        f"precis_web module(s) {new} contain raw SQL but have no "
        "tests/precis_web/test_<module>_sql.py running it on real Postgres "
        f"(grandfathered baseline: {len(_GRANDFATHERED)} modules)"
    )


def test_grandfathered_entries_are_still_missing() -> None:
    fixed = sorted(_GRANDFATHERED - _missing_companions())
    assert not fixed, (
        f"companion now exists / SQL gone; delete from _GRANDFATHERED: {fixed}"
    )
