"""Schema-drift guard — static SQL in the code must resolve against the schema.

Catches the class of bug where a migration renames or drops a table /
column but a hand-written query elsewhere still references the old name.
Real regressions this guard reproduces (all fixed 2026-06-19):

* ``cli/stats.py`` — ``ORDER BY created_at`` on ``ref_events`` (column is ``ts``).
* ``cli/dedupe.py`` — ``SELECT id, slug FROM refs`` (now ``ref_id`` + ``ref_identifiers``).
* ``utils/bib_gen.py`` — ``src.slug`` on ``refs`` (slug moved to ``ref_identifiers``).
* ``cli/maintenance.py`` — ``_VACUUM_TABLES`` listing pre-v2 phantom tables.

Mechanism
---------
AST-walk ``src/precis`` for ``.execute()`` / ``.executemany()`` calls whose
SQL argument is *statically reconstructable* — a string literal, ``+``- or
implicitly-concatenated literals, a module-level string constant, or an
f-string whose interpolations all resolve to module-level string constants
(e.g. the shared ``_REFS_COLS`` column lists, gathered across the package in
a pure-AST pre-pass — no modules are imported). Every reconstructable
SELECT / INSERT / UPDATE / DELETE / WITH is ``EXPLAIN``-ed against the
migrated test DB, with placeholders bound to NULL, inside a rolled-back
transaction (``EXPLAIN`` plans but never executes).

Only "undefined table / column / function / object" errors count as drift.
Type-inference and other planner errors are treated as *inconclusive* and
ignored — the guard asserts that schema references resolve, not that the
query would run with real arguments.

A second check validates module-level ``*_TABLES`` string collections (which
feed ``psycopg.sql.Identifier`` and never reach ``EXPLAIN``) against the live
table set, covering the ``_VACUUM_TABLES`` class.

Queries built from ``psycopg.sql`` composables or runtime-dynamic fragments
are skipped and counted; the test prints coverage so silent under-extraction
is visible.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest

from precis.store import Store

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
PRECIS_ROOT = SRC_ROOT / "precis"

# SQLSTATE codes that mean "the schema object this SQL names does not exist".
_DRIFT_CODES = {
    "42703",  # undefined_column
    "42P01",  # undefined_table
    "42883",  # undefined_function
    "42704",  # undefined_object
}

_EXPLAINABLE = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "VALUES", "TABLE")

# %%  -> literal percent (no placeholder);  %s/%b -> positional;  %(name)s -> named.
_NAMED_RE = re.compile(r"%\((\w+)\)[sb]")


@dataclass
class Query:
    sql: str
    file: str
    line: int


@dataclass
class Extraction:
    queries: list[Query] = field(default_factory=list)
    table_lists: list[tuple[str, list[str], str, int]] = field(default_factory=list)
    skipped_dynamic: int = 0


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _reconstruct(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Return the static SQL string for ``node`` or None if not fully static.

    ``consts`` is a name -> string-constant table (module-level constants
    gathered across the whole package, e.g. the shared ``_REFS_COLS`` column
    lists) used to resolve bare-name references and f-string interpolations.
    Resolution is purely static — no modules are imported, so the guard has
    zero import-time side effects on the rest of the test session.
    """
    direct = _const_str(node)
    if direct is not None:
        return direct
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _reconstruct(node.left, consts)
        right = _reconstruct(node.right, consts)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.JoinedStr):  # f-string
        out: list[str] = []
        for part in node.values:
            text = _const_str(part)
            if text is not None:
                out.append(text)
                continue
            if isinstance(part, ast.FormattedValue):
                inner = part.value
                if isinstance(inner, ast.Name) and inner.id in consts:
                    out.append(consts[inner.id])
                    continue
                return None  # dynamic interpolation — not static
            return None
        return "".join(out)
    return None


def _module_consts(tree: ast.Module, consts: dict[str, str]) -> None:
    """Record module-level ``NAME = <str>`` assignments into ``consts``."""
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            tgt = stmt.targets[0]
            if isinstance(tgt, ast.Name):
                val = _reconstruct(stmt.value, consts)
                if val is not None:
                    consts[tgt.id] = val


def _collect_file(path: Path, ext: Extraction, consts: dict[str, str]) -> None:
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return

    rel = str(path.relative_to(PRECIS_ROOT))

    # *_TABLES collections of string literals -> validate as table names.
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            tgt = stmt.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id.endswith("_TABLES"):
                if isinstance(stmt.value, ast.Tuple | ast.List | ast.Set):
                    names = [_const_str(e) for e in stmt.value.elts]
                    if names and all(n is not None for n in names):
                        ext.table_lists.append(
                            (tgt.id, [n for n in names if n], rel, stmt.lineno)
                        )

    # .execute()/.executemany() SQL arguments.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany")
        ):
            continue
        if not node.args:
            continue
        sql = _reconstruct(node.args[0], consts)
        if sql is None:
            # Only count the ones that plausibly *are* SQL (string-ish) as
            # skipped-dynamic; ignore unrelated .execute() calls.
            first = node.args[0]
            if isinstance(first, ast.JoinedStr | ast.BinOp | ast.Call):
                ext.skipped_dynamic += 1
            continue
        ext.queries.append(Query(sql=sql, file=rel, line=node.lineno))


def _extract_all() -> Extraction:
    paths = [
        p for p in sorted(PRECIS_ROOT.rglob("*.py")) if "/migrations/" not in str(p)
    ]
    # First pass: gather every module-level string constant across the package
    # so cross-module references (e.g. finding.py interpolating _mappers'
    # _REFS_COLS) resolve. Names are package-unique in practice.
    consts: dict[str, str] = {}
    for path in paths:
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        _module_consts(tree, consts)
    # Second pass: extract queries + table-name constants using that table.
    ext = Extraction()
    for path in paths:
        _collect_file(path, ext, consts)
    return ext


def _leading_keyword(sql: str) -> str:
    s = sql.lstrip()
    # strip leading line comments
    while s.startswith("--"):
        s = s.split("\n", 1)[1].lstrip() if "\n" in s else ""
    m = re.match(r"[A-Za-z]+", s)
    return m.group(0).upper() if m else ""


def _bind_params(sql: str) -> tuple | dict | None:
    """Return a NULL-bound parameter set matching ``sql``'s placeholders."""
    named = set(_NAMED_RE.findall(sql))
    if named:
        return {n: None for n in named}
    # positional: drop %% then count %s / %b
    stripped = sql.replace("%%", "")
    n = len(re.findall(r"%[sb]", stripped))
    return tuple(None for _ in range(n)) if n else None


@pytest.fixture(scope="module")
def _extraction() -> Extraction:
    return _extract_all()


def test_static_sql_resolves_against_schema(
    store: Store, _extraction: Extraction
) -> None:
    """Every reconstructable SQL string must name only live tables/columns."""
    ext = _extraction
    explainable = [q for q in ext.queries if _leading_keyword(q.sql) in _EXPLAINABLE]

    # Sanity: the extractor must actually find a meaningful corpus, otherwise
    # a refactor could make the guard silently pass by extracting nothing.
    assert len(explainable) >= 80, (
        f"extracted only {len(explainable)} explainable queries — "
        "extractor likely broke"
    )

    dsn = store.dsn
    assert dsn is not None

    failures: list[str] = []
    inconclusive = 0
    checked = 0
    # Dedicated autocommit connection (opened + closed here, NOT borrowed from
    # the shared pool): a failed EXPLAIN auto-aborts its own implicit
    # transaction, so we never leave an aborted tx or mutate a pooled
    # connection's autocommit flag — both of which deadlock later DB tests.
    with psycopg.connect(dsn, autocommit=True) as conn:
        for q in explainable:
            params = _bind_params(q.sql)
            try:
                conn.execute("EXPLAIN " + q.sql, params)
                checked += 1
            except psycopg.Error as exc:
                code = getattr(getattr(exc, "diag", None), "sqlstate", None)
                if code in _DRIFT_CODES:
                    failures.append(
                        f"{q.file}:{q.line}  [{code}] {str(exc).strip().splitlines()[0]}"
                    )
                else:
                    inconclusive += 1

    print(
        f"\nschema-drift: {checked} queries validated, {inconclusive} inconclusive "
        f"(type/other), {ext.skipped_dynamic} dynamic-skipped"
    )
    assert not failures, (
        "stale SQL references non-existent schema objects:\n" + "\n".join(failures)
    )


def test_table_name_constants_exist(store: Store, _extraction: Extraction) -> None:
    """Module-level ``*_TABLES`` string lists must name real tables/views."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ).fetchall()
    live = {r[0] for r in rows}

    failures: list[str] = []
    for name, tables, file, line in _extraction.table_lists:
        for t in tables:
            if t not in live:
                failures.append(f"{file}:{line}  {name} references missing table '{t}'")

    assert not failures, "stale table-name constants:\n" + "\n".join(failures)


def test_guard_detects_injected_drift(store: Store) -> None:
    """Meta-test: the EXPLAIN mechanism actually flags a known-bad reference."""
    bad = "SELECT slug FROM refs WHERE id = %s"  # both columns gone in v2
    dsn = store.dsn
    assert dsn is not None
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error) as ei:
            conn.execute("EXPLAIN " + bad, _bind_params(bad))
    assert getattr(ei.value.diag, "sqlstate", None) in _DRIFT_CODES
