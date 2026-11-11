"""scripts/ship refuses a branch that modifies or deletes a sealed migration.

gr632897: 5129265af rewrote a comment in src/precis_se/migrations/0016_*.sql via
the quick lane; the ledger checksum then failed ``precis migrate`` at deploy.
The guard is host-side git in scripts/ship (the warm gate has no .git), covers
every package's ``src/<pkg>/migrations/`` dir, and lets new files through.
Reuses the real-git rig of test_ship_narrow_lock; see that module's docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _finish, _tree_files

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

_SE = "src/precis_se/migrations/0016_se_hexfold_catalogue.sql"
_CORE = "src/precis/migrations/0001_initial.sql"
_BASELINE = "src/precis/migrations/baseline/schema.sql"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    r.race(
        {
            _SE: "-- see docs/backlog/hexfold-integration.md\nSELECT 1;\n",
            _CORE: "SELECT 1;\n",
            _BASELINE: "-- generated\n",
        }
    )
    # Worktrees branch from the primary's HEAD: it must carry the sealed files.
    r.g(r.primary, "pull", "-q", "--ff-only", "origin", "main")
    return r


def _refused(rig: Rig, wt: Path, path: str) -> None:
    main_before = rig.origin_ref("main")
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "SEALED migration" in out, out
    assert path in out, out
    assert rig.origin_ref("main") == main_before


@pytest.mark.parametrize("path", [_SE, _CORE])
def test_modified_sealed_migration_is_refused_in_any_package(
    rig: Rig, path: str
) -> None:
    # The gr632897 shape: a comment-only rewrite of an applied migration.
    wt = rig.worktree(
        "m", {"ok.txt": "fine\n", path: "-- see docs/backlog/other.md\nSELECT 1;\n"}
    )
    _refused(rig, wt, path)


def test_deleted_sealed_migration_is_refused(rig: Rig) -> None:
    wt = rig.worktree("d", {"ok.txt": "fine\n"})
    rig.g(wt, "rm", "-q", _SE)
    rig.g(wt, "commit", "-q", "-m", "drop it")
    _refused(rig, wt, _SE)


def test_renamed_sealed_migration_is_refused(rig: Rig) -> None:
    wt = rig.worktree("r", {"ok.txt": "fine\n"})
    rig.g(wt, "mv", _SE, "src/precis_se/migrations/0016_renamed.sql")
    rig.g(wt, "commit", "-q", "-m", "rename it")
    _refused(rig, wt, _SE)


def test_new_migration_and_baseline_regen_pass(rig: Rig) -> None:
    new = "src/precis_se/migrations/0017_next.sql"
    wt = rig.worktree("n", {new: "SELECT 2;\n", _BASELINE: "-- regenerated\n"})
    out = _finish(*rig.ship(wt, "--quick", sleep=1))
    assert "SEALED migration" not in out, out
    assert new in _tree_files(rig, "main")


def test_override_env_allows_deliberate_ledger_repair(rig: Rig) -> None:
    wt = rig.worktree("o", {_SE: "-- repaired\nSELECT 1;\n"})
    out = _finish(
        *rig.ship(wt, "--quick", sleep=1, PRECIS_SHIP_ALLOW_SEALED_MIGRATION_EDIT="1")
    )
    assert "SEALED migration" not in out, out
