"""Unit tests for the `/qgo` irreversibility guard's decision logic.

Pure — exercises ``evaluate`` with path lists, no git and no filesystem.
Mirrors ``tests/test_cd_to_primary_guard.py``'s pattern for loading a
hyphenated-name script by path.

The guard's value is entirely in what it refuses and what it lets through, so
both directions are pinned here: a migration or an SSRF-boundary change must
refuse, and ordinary code — including code that merely *looks* risky — must
not, because a guard that creeps rebuilds the gate `/qgo` exists to skip.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "qgo-guard"


def _load() -> ModuleType:
    # Load a .py copy: testmon fingerprints every module it sees, and a dotless
    # path crashes its get_file (gr450298).
    copy = Path(tempfile.mkdtemp(prefix="qgo_guard_")) / "qgo_guard.py"
    shutil.copyfile(_SCRIPT, copy)
    spec = importlib.util.spec_from_loader(
        "qgo_guard",
        importlib.machinery.SourceFileLoader("qgo_guard", str(copy)),
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: ``@dataclass`` resolves ``cls.__module__`` through
    # ``sys.modules`` while decorating, and fails on a module that isn't there.
    sys.modules["qgo_guard"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    return _load()


# ── refusals ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "src/precis/migrations/0049_something.sql",
        "src/precis_se/migrations/0016_hexfold_cache.sql",
        "src/precis_bio/migrations/0003_x.sql",
        "src/precis_chem/migrations/0002_x.sql",
        "src/precis_estimate/migrations/0001_x.sql",
        "src/precis_pathway/migrations/0007_x.sql",
    ],
)
def test_a_migration_in_any_package_refuses(guard: ModuleType, path: str) -> None:
    refusal = guard.evaluate([path])
    assert refusal is not None
    assert path in refusal.paths
    assert "forward-only" in refusal.reason


def test_a_migration_in_a_package_that_does_not_exist_yet_refuses(
    guard: ModuleType,
) -> None:
    """Matched on path shape, not an enumerated package list — a seventh
    plugin must not fall through the guard on the day it is created."""
    refusal = guard.evaluate(["src/precis_future/migrations/0001_new.sql"])
    assert refusal is not None


def test_safe_fetch_refuses(guard: ModuleType) -> None:
    refusal = guard.evaluate(["src/precis/utils/safe_fetch.py"])
    assert refusal is not None
    assert "SSRF" in refusal.reason


def test_safe_fetch_refuses_after_a_move(guard: ModuleType) -> None:
    """Suffix-matched so relocating the module keeps the guard armed."""
    refusal = guard.evaluate(["src/precis/net/utils/safe_fetch.py"])
    assert refusal is not None


def test_a_migration_wins_over_safe_fetch_in_the_message(guard: ModuleType) -> None:
    """Both present: the refusal names the migration, the less recoverable of
    the two — a redeploy fixes safe_fetch, nothing un-applies a migration."""
    refusal = guard.evaluate(
        ["src/precis/utils/safe_fetch.py", "src/precis/migrations/0049_x.sql"]
    )
    assert refusal is not None
    assert refusal.paths == ("src/precis/migrations/0049_x.sql",)


def test_refusal_renders_the_paths_and_points_at_go(guard: ModuleType) -> None:
    rendered = guard.evaluate(["src/precis/migrations/0049_x.sql"]).render()
    assert "src/precis/migrations/0049_x.sql" in rendered
    assert "/go" in rendered


# ── the far more important direction: what must NOT refuse ─────────────────


@pytest.mark.parametrize(
    "path",
    [
        "src/precis/workers/hub_refine.py",
        "src/precis_se/handler.py",
        "src/precis_web/routes/refs.py",
        "src/hexfold/join.py",
        "docs/backlog/anything.md",
        "tests/test_se_chain_ops.py",
        "scripts/ship",
        "deploy/roles/precis_embedder/tasks/main.yml",
    ],
)
def test_ordinary_changes_do_not_refuse(guard: ModuleType, path: str) -> None:
    assert guard.evaluate([path]) is None


def test_nothing_changed_does_not_refuse(guard: ModuleType) -> None:
    assert guard.evaluate([]) is None


def test_a_python_file_inside_a_migrations_package_does_not_refuse(
    guard: ModuleType,
) -> None:
    """Only the sealed ``.sql`` is irreversible. ``migrations/__init__.py`` is
    ordinary code and redeploys like anything else."""
    assert guard.evaluate(["src/precis_se/migrations/__init__.py"]) is None


def test_a_doc_that_merely_mentions_migrations_does_not_refuse(
    guard: ModuleType,
) -> None:
    assert guard.evaluate(["docs/runbooks/migration-collision.md"]) is None


def test_a_test_for_safe_fetch_does_not_refuse(guard: ModuleType) -> None:
    """Testing the SSRF boundary is not changing it."""
    assert guard.evaluate(["tests/test_safe_fetch.py"]) is None
