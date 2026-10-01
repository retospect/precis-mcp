"""The plugin lattice: core may not import a plugin, and no migration may
reference another package's tables.

Step 1 of ``docs/backlog/plugin-split-runtime-shell.md`` — encode the
boundary *before* anything moves, so the 2026-10-16 extractions are verified
against a gate rather than against a reviewer's attention. The split's whole
premise is that `pip install precis-util <one-model>` works, which it cannot
if core reaches into a model.

Why a test and not a convention. The convention was already written down
(``0162_design_core.sql``: a block "lives in a PLUGIN table ... in a
different migration namespace, which core may not reference") and already
practised (``quest/compute.py`` orchestrates autocatpath by job_type *name*
and plain dicts, with the no-import rule restated at seven separate call
sites). It still broke twice in four days: ``quest/figures.py`` and
``quest/results_table.py`` imported ``precis_pathway.analysis`` until
2026-09-29, and ``quest/roadmap_tick.py`` began importing
``precis_se.handler`` by 2026-10-01 (gr459054). Both were *function-local*
imports, which is what makes attention the wrong instrument: they are
invisible at module import, they fire only on the branch that happens to
run, and the blast radius grows with a process's uptime (gr457894).

The plugin set is read from ``pyproject.toml``'s entry-point groups rather
than hardcoded, so registering a new model puts it under the boundary
automatically — a hardcoded list would silently exempt exactly the packages
most likely to get this wrong, the new ones.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_CORE = _SRC / "precis"

#: Entry-point groups whose values name a plugin module. A package appearing
#: in any of these is a plugin for boundary purposes.
_PLUGIN_GROUPS = (
    "precis.handlers",
    "precis.job_types",
    "precis.migrations",
    "precis.handle_codes",
    "precis.skills",
)

#: Known core→plugin imports, each with the gripe that owns the fix. An entry
#: here keeps this test green while leaving the violation *counted and
#: attributed* rather than invisible — the grandfathering `package-split.md`
#: asks for. Delete the entry when the gripe lands; do not add one without a
#: gripe id, and never to make a new import pass.
_GRANDFATHERED: dict[str, str] = {
    "precis/quest/roadmap_tick.py": (
        "gr459054 — _se_measures_text imports SeHandler to render a part's "
        "measures view. The fix is threading the booted hub down so "
        "hub.sibling('se') can resolve it by name; the file is the quest "
        "thread's and under active edit."
    ),
}


def _plugin_packages() -> set[str]:
    """Top-level packages registered as plugins in ``pyproject.toml``."""
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = data.get("project", {}).get("entry-points", {})
    packages: set[str] = set()
    for group in _PLUGIN_GROUPS:
        for target in groups.get(group, {}).values():
            # "precis_se.handler:SeHandler" and "precis_se.migrations" both
            # start with the package.
            packages.add(target.split(":")[0].split(".")[0])
    return packages


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported anywhere in a file, including inside a
    function body — the function-local case is the one that bites."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module=None; relative imports never
            # cross a package boundary, so they are not our concern.
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def test_core_does_not_import_a_plugin() -> None:
    plugins = _plugin_packages()
    assert plugins, "no plugin entry points found — the parse is wrong"

    offenders: list[str] = []
    for path in sorted(_CORE.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for module in sorted(_imported_modules(path)):
            root = module.split(".")[0]
            if root not in plugins:
                continue
            if rel in _GRANDFATHERED:
                continue
            offenders.append(f"{rel}: imports {module}")

    assert not offenders, (
        "core (src/precis) must not import a plugin package — "
        "`pip install precis-util <one-model>` has to work without the "
        "others installed. Reach the plugin by kind through the Hub, or "
        "orchestrate it by job_type name as quest/compute.py does. "
        f"Offenders: {offenders}"
    )


def test_grandfathered_entries_still_violate() -> None:
    """A stale allowlist is worse than none: it reads as a known-bad list
    while quietly exempting a file that is now clean, so the next real
    violation in that file lands green. Every entry must still be a live
    violation."""
    plugins = _plugin_packages()
    stale: list[str] = []
    for rel, why in _GRANDFATHERED.items():
        path = _SRC / rel
        if not path.exists():
            stale.append(f"{rel}: file is gone — drop the entry ({why})")
            continue
        if not any(
            module.split(".")[0] in plugins for module in _imported_modules(path)
        ):
            stale.append(f"{rel}: no longer imports a plugin — drop the entry")
    assert not stale, f"_GRANDFATHERED is out of date: {stale}"


def _tables_by_package() -> dict[str, set[str]]:
    """``{package: {table, ...}}`` from every ``CREATE TABLE`` in each
    package's migrations. Core is keyed as ``precis``."""
    create = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)",
        re.IGNORECASE,
    )
    tables: dict[str, set[str]] = {}
    for migrations in sorted(_SRC.glob("*/migrations")):
        package = migrations.parent.name
        owned: set[str] = set()
        for sql in sorted(migrations.glob("*.sql")):
            owned.update(
                m.group(1).lower()
                for m in create.finditer(sql.read_text(encoding="utf-8"))
            )
        if owned:
            tables[package] = owned
    return tables


def _sql_boundary_offenders(owner: str, foreign: dict[str, set[str]]) -> list[str]:
    """Every reference in ``owner``'s migrations to a table some other
    package creates."""
    migrations = _SRC / owner / "migrations"
    offenders: list[str] = []
    for sql in sorted(migrations.glob("*.sql")):
        body = sql.read_text(encoding="utf-8")
        # Strip comments AND single-quoted literals before matching. Several
        # core migrations *discuss* a plugin table by name precisely to
        # explain why they must not reference it — 0162_design_core.sql on
        # `se_blocks` in a comment, 0158_checklist_kind.sql on `se_notes`
        # inside a `COMMENT ON ... IS '...'` string. That prose is the
        # convention this test encodes, not a breach of it; a real reference
        # is executable SQL, which is neither.
        code = re.sub(r"--[^\n]*", "", body)
        code = re.sub(r"'(?:[^']|'')*'", "''", code)
        for other, names in foreign.items():
            if other == owner:
                continue
            for table in sorted(names):
                if re.search(rf"\b{re.escape(table)}\b", code, re.IGNORECASE):
                    offenders.append(f"{sql.name}: references {other}'s {table}")
    return offenders


def test_core_sql_does_not_reference_a_plugin_table() -> None:
    tables = _tables_by_package()
    plugins = {p: t for p, t in tables.items() if p in _plugin_packages()}
    assert plugins, "no plugin migrations found — the scan is wrong"

    offenders = _sql_boundary_offenders("precis", plugins)
    assert not offenders, (
        "a core migration references a plugin's table. Core runs first and "
        "the plugin may not be installed at all; the stable identity is a "
        "plain column, never an FK (see 0162_design_core.sql on "
        f"`block_uid`). Offenders: {offenders}"
    )


def test_a_plugin_sql_does_not_reference_another_plugins_table() -> None:
    tables = _tables_by_package()
    plugins = {p: t for p, t in tables.items() if p in _plugin_packages()}

    offenders: list[str] = []
    for package in sorted(plugins):
        offenders += [
            f"{package}/{o}" for o in _sql_boundary_offenders(package, plugins)
        ]

    assert not offenders, (
        "a plugin migration references another plugin's table. Install "
        "order is not guaranteed and `_migrations` is keyed (plugin, "
        f"version), so the referenced table may never exist. Offenders: {offenders}"
    )
