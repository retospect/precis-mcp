"""The checked-gate selection (``scripts/impacted-tests``) stays honest.

``--checked`` / ``--selected`` are only useful if the selection actually
contains the tests a diff can red. This file is the ratchet (gr485791):

- every CONTRACT / PATH_MAP target exists (node ids resolved by ast);
- every PATH_MAP pattern still matches a live repo path — a dead pattern is
  a silently dropped coupling;
- every test file whose corpus refs cover a changed path is in the
  selection for that path — a new snapshot/table test that reads a corpus
  dir goes red here until the coupling is real;
- the five r35 escapees are each selected from their offender's diff.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SEL_PATH = REPO / "scripts" / "impacted-tests"

# extension-less executable — hand SourceFileLoader an explicit loader, from
# a `.py`-suffixed COPY: pytest-testmon fingerprints executed files by
# extension and IndexErrors on one that has none (same trick as
# tests/test_coderef_structural.py).
_sel_copy = Path(tempfile.mkdtemp(prefix="select_tests_py_")) / "impacted_tests.py"
shutil.copyfile(SEL_PATH, _sel_copy)
_loader = importlib.machinery.SourceFileLoader("impacted_tests", str(_sel_copy))
_spec = importlib.util.spec_from_loader("impacted_tests", _loader)
assert _spec and _spec.loader
sel: Any = importlib.util.module_from_spec(_spec)
sys.modules["impacted_tests"] = sel  # slots=True dataclasses look up __module__
_spec.loader.exec_module(sel)


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _resolve(target: str) -> tuple[Path, str | None]:
    file, _, node = target.partition("::")
    return REPO / file, node or None


@pytest.fixture(scope="module")
def selector() -> sel.Selector:
    return sel.Selector(REPO)


def test_contract_targets_exist() -> None:
    for t in sel.CONTRACT:
        path, node = _resolve(t)
        assert path.exists(), f"CONTRACT target missing: {t}"
        if node:
            assert node in _test_functions(path), f"CONTRACT node missing: {t}"


def test_path_map_targets_exist() -> None:
    for pat, targets in sel.PATH_MAP:
        assert targets, f"PATH_MAP pattern {pat!r} has no targets"
        for t in targets:
            path, node = _resolve(t)
            assert path.exists(), f"{pat!r} target missing: {t}"
            if node:
                assert node in _test_functions(path), f"{pat!r} node missing: {t}"


def test_path_map_patterns_match_something() -> None:
    # no git in the gate container — walk the tree instead of ls-files
    skip = {".git", ".venv", "node_modules", "__pycache__"}
    tracked: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            tracked.append(str(Path(dirpath, f).relative_to(REPO)))
    for pat, _ in sel.PATH_MAP:
        assert any(sel._pattern_covers(pat, f) for f in tracked), (
            f"PATH_MAP pattern {pat!r} matches nothing — dead coupling, drop it"
        )


def test_every_corpus_ref_is_selectable(selector: sel.Selector) -> None:
    """Every repo path a test file reads must select that test when it
    changes — the snapshot/table/registry tests can never sit outside the
    selection. Excludes refs under tests/ (the changed-test-file rule
    covers those directly) and the repo root (a whole-tree scan is CONTRACT
    by definition)."""
    selector.build_corpus()
    misses: list[str] = []
    contract_files = {_resolve(t)[0] for t in sel.CONTRACT}
    for tpath, refs in selector.test_refs.items():
        trel = str(tpath.relative_to(REPO))
        for ref in refs:
            if ref == "tests" or ref.startswith("tests/"):
                continue
            if sel._is_full_trigger(ref):
                # harness/schema paths select everything (FULL) — coverage
                # is total by construction, no subset to prove here
                continue
            targets, _ = selector.select([ref])
            if trel not in {str(_resolve(t)[0].relative_to(REPO)) for t in targets}:
                misses.append(f"{trel} reads {ref}")
    assert not misses, "corpus-coupled tests not reachable:\n" + "\n".join(
        sorted(misses)
    )


# The five r35 reds, by offender diff (gr485791). Python-only cases may be
# delegated to testmon when a map exists — `testmon or want in targets`.
R35_CASES = [
    # call-errors: advertised sort hint changed -> numeric browse sort test
    (["src/precis/tools/core.py"], "tests/test_numeric_browse_sort.py"),
    # ...and the same hint edited where it lives, with no core.py change
    (
        ["src/precis/handlers/_numeric_ref.py"],
        "tests/test_numeric_browse_sort.py",
    ),
    # call-errors: memory edit mode='append' -> audited mode table
    (["src/precis/handlers/memory.py"], "tests/test_mode_spec_parity.py"),
    # gardener: skill family 'maintenance' -> closed-set check
    (
        ["src/precis/data/skills/precis-gardener-help.md"],
        "tests/test_skill_refs.py",
    ),
    # h1-tunnel: new .hx example -> seam snapshot table
    (["hexfold/examples/tunnel_reservoirs.hx"], "tests/hexfold/test_seam.py"),
    # gardener: reviewed-blob pin edited -> mirror acceptance
    (["src/precis/utils/text.py"], "tests/test_memory_mirror_acceptance.py"),
]


@pytest.mark.parametrize(("changed", "want"), R35_CASES)
def test_r35_regressions_are_selected(selector: sel.Selector, changed, want) -> None:
    targets, testmon = selector.select(changed)
    if all(f.endswith(".py") for f in changed):
        assert (
            testmon
            or want in targets
            or any(t.startswith(want + "::") for t in targets)
        ), f"{want} not selected for {changed}"
    else:
        assert want in targets or any(t.startswith(want + "::") for t in targets), (
            f"{want} not selected for {changed}"
        )


def test_changed_test_file_selects_itself(selector: sel.Selector) -> None:
    targets, _ = selector.select(["tests/test_skill_prose.py"])
    assert "tests/test_skill_prose.py" in targets


def test_deleted_module_still_selects_importers(selector: sel.Selector) -> None:
    """A module deleted by the diff isn't in the file map anymore — its
    importers must still be selected (they break at import time)."""
    del selector.mod2file["precis.utils.link_target"]
    targets, testmon = selector.select(["src/precis/utils/link_target.py"])
    assert testmon or "tests/test_link_crud.py" in targets


def test_never_selects_everything(selector: sel.Selector) -> None:
    """A leaf-module diff must stay a fraction of the suite — the whole
    point of the mode is that it is cheaper than the full gate."""
    targets, _ = selector.select(["src/precis/utils/link_target.py"])
    all_tests = sorted((REPO / "tests").rglob("test_*.py"))
    assert len(targets) < len(all_tests) // 2
