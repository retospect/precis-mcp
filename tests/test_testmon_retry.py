"""scripts/lib/testmon-retry.sh: retry only on testmon's own INTERNALERROR."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs bash scripts/lib/testmon-retry.sh",
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "testmon-retry.sh"


def _run(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    data = tmp_path / ".testmondata"
    data.write_text("stale", encoding="utf-8")
    Path(str(data) + "-wal").write_text("x", encoding="utf-8")
    r = subprocess.run(
        ["bash", str(SCRIPT), str(data), "bash", "-c", body],
        capture_output=True,
        encoding="utf-8",
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "TMPDIR": str(tmp_path),
        },
    )
    return r, data


def test_testmon_internalerror_drops_map_but_stays_red(tmp_path: Path) -> None:
    """The stale map is dropped for the next run, but this run still fails:
    testmon's selected tests did not run, so a green would be unearned."""
    body = "echo 'INTERNALERROR> File testmon_core.py, in get_file'; exit 3"
    r, data = _run(tmp_path, body)
    assert r.returncode == 3
    assert "re-run" in r.stderr
    assert not data.exists()  # no empty map left behind: next run sees "no map"
    assert not Path(str(data) + "-wal").exists()
    assert Path(str(data) + ".dropped").exists()  # tells ship to drop the host copy


@pytest.mark.parametrize(
    "body, rc",
    [
        ("echo '1 failed'; exit 1", 1),
        ("echo 'INTERNALERROR> some other plugin blew up'; exit 3", 3),
    ],
)
def test_other_reds_still_fail(tmp_path: Path, body: str, rc: int) -> None:
    r, data = _run(tmp_path, body)
    assert r.returncode == rc
    assert data.read_text(encoding="utf-8") == "stale"
    assert not Path(str(data) + ".dropped").exists()


def test_green_passes(tmp_path: Path) -> None:
    r, data = _run(tmp_path, "echo ok")
    assert r.returncode == 0
    assert data.read_text(encoding="utf-8") == "stale"


def test_ship_drops_host_map_when_container_marked_dropped(tmp_path: Path) -> None:
    """testmon_from_container removes the host map when the dropped marker exists."""
    ship = SCRIPT.parents[1] / "ship"
    text = ship.read_text(encoding="utf-8")
    start = text.index("testmon_from_container() {")
    end = text.index("\n}\n", start) + 3
    func = text[start:end]
    for name in (".testmondata", ".testmondata-wal"):
        (tmp_path / name).write_text("stale", encoding="utf-8")
    # compose stub: every in-container command "succeeds" (marker present).
    script = f"WORKTREE={tmp_path}\ncompose() {{ return 0; }}\n{func}\ntestmon_from_container\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert not list(tmp_path.glob(".testmondata*"))
