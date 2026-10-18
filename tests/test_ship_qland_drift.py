"""ship --quick's qland drift guard: warn at 24 h, refuse at 48 h.

The guard (scripts/ship) asks ``scripts/last-gated-main-sha --age-hours``
how stale main's last all-green shard matrix is. Reuses the real-git rig of
test_ship_narrow_lock (origin.git, primary, worktrees); see that module's
docstring. The rig stages a stub ``last-gated-main-sha`` that answers "age
unknown" (empty stdout, exit 0); each test here commits its own stub into
the worktree — one that prints a chosen age, or exits 2 — so the real ship
script exercises each guard branch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _finish, _tree_files

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _drift_wt(rig: Rig, name: str, stub: str) -> Path:
    """A worktree whose commit swaps in a stub ``last-gated-main-sha``."""
    return rig.worktree(
        name, {"scripts/last-gated-main-sha": stub, f"{name}.txt": f"{name}\n"}
    )


def test_warns_past_24h_and_still_ships(rig: Rig) -> None:
    wt = _drift_wt(rig, "warn", "#!/bin/sh\necho 30\n")
    p, log = rig.ship(wt, "--quick", sleep=1)
    out = _finish(p, log)
    assert "main's last shard verdict is 30h old" in out, out
    assert "a /go is due" in out, out
    assert "warn.txt" in _tree_files(rig)


def test_refuses_at_48h(rig: Rig) -> None:
    wt = _drift_wt(rig, "old", "#!/bin/sh\necho 48\n")
    main0 = rig.origin_ref("main")
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "last all-green shard matrix is 48h old (refuse at 48h)" in out, out
    assert "PRECIS_QLAND_DRIFT_OVERRIDE=1" in out, out
    assert rig.origin_ref("main") == main0
    assert not (rig.lintdir / "old.started").exists()


def test_exit_2_no_green_verdict_refuses(rig: Rig) -> None:
    """Exit 2 is an answer, not a crash: the whole window was walked and no
    sha in it is green — the refusal the empty-output cases must not mask."""
    wt = _drift_wt(rig, "none", "#!/bin/sh\nexit 2\n")
    main0 = rig.origin_ref("main")
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "no all-green shard matrix in the last 48h" in out, out
    assert rig.origin_ref("main") == main0
    assert not (rig.lintdir / "none.started").exists()


def test_unknown_age_never_refuses(rig: Rig) -> None:
    """Empty stdout + exit 0 = could not look (no gh, no auth, API error).
    An unanswerable lookup must never block a ship, or a GitHub outage
    stops the fleet."""
    wt = _drift_wt(rig, "unk", "#!/bin/sh\nexit 0\n")
    p, log = rig.ship(wt, "--quick", sleep=1)
    out = _finish(p, log)
    assert "age unknown" in out, out
    assert "unk.txt" in _tree_files(rig)


@pytest.mark.parametrize(
    "stub",
    ["#!/bin/sh\necho 50\n", "#!/bin/sh\nexit 2\n"],
    ids=["verdict-too-old", "no-verdict-in-window"],
)
def test_drift_override_lands(rig: Rig, stub: str) -> None:
    wt = _drift_wt(rig, "ovr", stub)
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_QLAND_DRIFT_OVERRIDE="1")
    out = _finish(p, log)
    assert "REFUSAL OVERRIDDEN" in out, out
    assert "ovr.txt" in _tree_files(rig)
