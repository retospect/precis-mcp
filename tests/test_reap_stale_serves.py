"""``scripts/reap-stale-serves`` — the replaced-session-MCP sweep.

On ``/mcp`` reconnect, Claude Code spawns a fresh stdio server but never
closes the old one's stdin socketpair, so the old server blocks forever in
``read()`` at ~250–500 MB RSS. The sweep's criterion: among ``precis
serve`` processes SHARING a ppid, only the youngest is the session's live
server — every older sibling has been replaced. Singletons are live
sessions and must never be touched.

Exercises the REAL script (never reimplemented) against a synthetic
process tree: parent shells whose argv does NOT contain the match string
spawn fake serve processes whose argv DOES (via ``PRECIS_REAP_SERVE_CMD``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

#: The sweep is a bash script over a POSIX `ps` process tree, and the fixture
#: backgrounds children from bash — Windows can't even exec the shebang
#: script (WinError 193). The SessionStart hook under test only ever runs on
#: POSIX dev hosts; the Linux/macOS CI legs keep it covered.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only bash/ps process-tree sweep"
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reap-stale-serves"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def fake_tree(tmp_path: Path) -> Iterator[dict[str, object]]:
    """A pair-parent (old + young fake serve) and a singleton-parent.

    The fake-serve path is the match string; parents hardcode it in their
    script body so their own argv stays clean, mirroring how the real
    Claude session process doesn't itself match.
    """
    fake = tmp_path / "fake-serve"
    # `exec -a "$0"` keeps the fake's path as argv[0] (a bare `exec sleep`
    # would erase the match string from ps, and the sweep would see nothing).
    fake.write_text('#!/bin/bash\nexec -a "$0" sleep 300\n', encoding="utf-8")
    fake.chmod(0o755)

    pid_file = tmp_path / "pids"
    pair_parent = tmp_path / "pair-parent.sh"
    # The older child must be visibly older: ps etime has 1 s granularity.
    pair_parent.write_text(
        f'"{fake}" & echo "old=$!" >> "{pid_file}"\n'
        "sleep 2\n"
        f'"{fake}" & echo "young=$!" >> "{pid_file}"\n'
        "wait\n",
        encoding="utf-8",
    )
    single_parent = tmp_path / "single-parent.sh"
    single_parent.write_text(
        f'"{fake}" & echo "single=$!" >> "{pid_file}"\nwait\n',
        encoding="utf-8",
    )

    procs = [
        subprocess.Popen(["bash", str(pair_parent)]),
        subprocess.Popen(["bash", str(single_parent)]),
    ]
    deadline = time.monotonic() + 10
    pids: dict[str, int] = {}
    while time.monotonic() < deadline and len(pids) < 3:
        if pid_file.exists():
            pids = {
                k: int(v)
                for k, v in (
                    line.split("=")
                    for line in pid_file.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
            }
        time.sleep(0.05)
    assert len(pids) == 3, f"synthetic tree failed to start: {pids}"

    yield {"pids": pids, "match": str(fake)}

    for p in procs:
        p.terminate()
    for pid in pids.values():
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    for p in procs:
        p.wait(timeout=10)


def _run(match: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        env={**os.environ, "PRECIS_REAP_SERVE_CMD": match},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_dry_run_names_only_the_replaced_sibling(
    fake_tree: dict[str, object],
) -> None:
    pids: dict[str, int] = fake_tree["pids"]  # type: ignore[assignment]
    res = _run(str(fake_tree["match"]), "--dry-run")
    assert res.returncode == 0
    assert f"{pids['old']}" in res.stdout
    assert f"{pids['young']}" not in res.stdout
    assert f"{pids['single']}" not in res.stdout
    # dry-run kills nothing
    assert all(_alive(p) for p in pids.values())


def test_reaps_the_replaced_sibling_and_spares_live_servers(
    fake_tree: dict[str, object],
) -> None:
    pids: dict[str, int] = fake_tree["pids"]  # type: ignore[assignment]
    res = _run(str(fake_tree["match"]))
    assert res.returncode == 0
    assert "reaped" in res.stdout
    assert _wait_gone(pids["old"])
    assert _alive(pids["young"]), "the session's live server must survive"
    assert _alive(pids["single"]), "a singleton is a live session — untouchable"


def test_escape_hatch_and_no_matches_are_quiet_noops(tmp_path: Path) -> None:
    res = subprocess.run(
        [str(SCRIPT)],
        env={
            **os.environ,
            "PRECIS_REAP_SERVE_CMD": str(tmp_path / "matches-nothing"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0
    assert "nothing to reap" in res.stdout

    res = subprocess.run(
        [str(SCRIPT)],
        env={**os.environ, "PRECIS_NO_AUTOREAP": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0
    assert res.stdout == ""
