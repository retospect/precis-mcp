"""Test modules that eagerly import an extra-only module must guard it.

The CI test lanes run ``--no-extra catalyst --no-extra catalyst-gpu``
(``.github/workflows/check.yml``), but the local ship gate's container has the
catalyst extra installed. So a test module that imports ``autocatpath`` — or
any ``src/`` module that imports it at module scope — passes the gate and
then fails **collection** of its whole CI shard with ``ModuleNotFoundError:
No module named 'autocatpath'``.
Collection failure, not test failure: one unguarded import reddens every test
in the shard. (``precis_pathway.runner`` was the offender until gr514717
lazified its engine surface; today no ``src/`` module infects.)

That happened on 2026-09-27: ``tests/test_pathway_kinetics_timeout.py`` landed
via an ungated ``/qland``, did ``from precis_pathway import runner`` at module
scope, and took out all 6 ``test-linux`` shards (run 36310584599) while the
local gate stayed green. Eight sibling modules already carried the
``pytest.importorskip("autocatpath")`` guard; this walk is what makes the gate
teach the rule instead of CI discovering it after the merge.

The infected set is **derived** from ``src/`` at run time rather than listed,
so a new extra-only module joins the rule automatically. Coverage is one hop
(test → src module → ``autocatpath``), which is the demonstrated failure shape;
a deeper chain would need an import graph and has not bitten yet.

Failure means: add the guard above the offending import. Every engine gate
goes through ``tests._autocatpath.needs_engine`` (gr514397) — a bare
``importorskip`` hides that the fix is an install::

    from tests._autocatpath import needs_engine

    needs_engine()

    from precis_pathway import runner
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_TESTS = _ROOT / "tests"

#: Distribution-level modules that only exist with an optional extra.
_EXTRA_ONLY = {"autocatpath"}

#: Modules a human vetted as safe despite tripping the detector.
_EXEMPT: set[str] = set()


def _module_scope_imports(tree: ast.AST) -> set[str]:
    """Top-level (not function/class-nested) imported root module names."""
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolved by caller
                continue
            if node.module:
                names.add(node.module.split(".")[0])
                names.add(node.module)
                # `from precis_pathway import runner` imports the submodule
                # `precis_pathway.runner`; without this spelling the walk
                # missed it and test_pathway_cpuset.py reddened all six
                # shards (2026-10-03, run 37128991031).
                for a in node.names:
                    names.add(f"{node.module}.{a.name}")
    return names


def _infected_src_modules(src_root: Path = _SRC) -> set[str]:
    """`src/` modules importing an extra-only module at module scope.

    Returned as importable dotted paths (``precis_pathway.runner``) plus the
    bare ``from X import Y`` spelling a test would use.
    """
    infected: set[str] = set()
    for path in src_root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - would fail elsewhere first
            continue
        roots = {n.split(".")[0] for n in _module_scope_imports(tree)}
        if not (roots & _EXTRA_ONLY):
            continue
        rel = path.relative_to(src_root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            infected.add(".".join(parts))
    return infected


def _guards_the_extra(source: str) -> bool:
    # needs_engine (tests/_autocatpath.py, gr514397) IS the guard — a
    # needs_engine() call skips the module the way a bare importorskip did,
    # but with the missing-extra reason attached. The importorskip spellings
    # stay accepted for the non-autocatpath extras this walk may grow.
    return any(
        "needs_engine(" in source
        or f'importorskip("{m}")' in source
        or f"importorskip('{m}')" in source
        or (m in source and "skipif" in source)
        for m in _EXTRA_ONLY
    )


def test_the_detector_is_not_vacuous(tmp_path: Path) -> None:
    """A refactor must not silently break the derivation.

    Since gr514717 no ``src/`` module imports an extra-only module at module
    scope (``precis_pathway.runner`` lazifies its engine names through PEP
    562), so the real set is legitimately empty and the detector is exercised
    on a synthetic tree instead: module-scope imports infect, a function-
    scope import and a TYPE_CHECKING block do not.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import autocatpath\n", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "from autocatpath.pipeline import run\n", encoding="utf-8"
    )
    (pkg / "clean.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from autocatpath.config import Config\n"
        "def f():\n"
        "    import autocatpath\n",
        encoding="utf-8",
    )
    infected = _infected_src_modules(tmp_path)
    assert infected == {"pkg", "pkg.mod"}, (
        f"the derivation missed the synthetic infected modules "
        f"(found: {sorted(infected)})"
    )


def test_test_modules_guard_their_extra_only_imports() -> None:
    infected = _infected_src_modules()
    bad: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.name in _EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        imported = _module_scope_imports(tree)
        hits = (imported & _EXTRA_ONLY) | (imported & infected)
        if hits and not _guards_the_extra(source):
            bad.append(f"{path.name}: imports {sorted(hits)} at module scope")
    assert not bad, (
        "test modules import an extra-only module at module scope without a "
        "guard — this fails COLLECTION of the whole CI shard under "
        "`--no-extra catalyst`:\n  " + "\n  ".join(bad)
    )
