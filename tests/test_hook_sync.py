"""scripts/hook-sync: REPORT drift between scripts/hooks/* and the live
~/.claude/hooks copies (gr516942).

The live dir and the settings.json that names the expected set are both
env-overridable (PRECIS_HOOKS_LIVE / PRECIS_HOOKS_SETTINGS), so the test
rig needs no real ~/.claude. The script only ever reports — the
assertions check the report lines and that the live dir is untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs the bash script"
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hook-sync"
REPO_HOOKS = ROOT / "scripts" / "hooks"


@pytest.fixture()
def rig(tmp_path: Path) -> dict[str, str]:
    live = tmp_path / "live"
    live.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    return {
        "PRECIS_HOOKS_LIVE": str(live),
        "PRECIS_HOOKS_SETTINGS": str(settings),
        "HOME": str(tmp_path),
    }


def _run(rig: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**os.environ, **rig},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def _live(rig: dict[str, str]) -> Path:
    return Path(rig["PRECIS_HOOKS_LIVE"])


def _copy_hook(rig: dict[str, str], name: str, text: str | None = None) -> Path:
    """Plant a live copy of a repo hook, optionally with altered content."""
    target = _live(rig) / name
    if text is None:
        target.write_bytes((REPO_HOOKS / name).read_bytes())
    else:
        target.write_text(text, encoding="utf-8")
    return target


def test_in_sync_reports_once(rig: dict[str, str]) -> None:
    _copy_hook(rig, "read-dedup.py")
    r = _run(rig)
    assert r.returncode == 0
    assert "in sync" in r.stdout
    assert "DIFFERS" not in r.stdout and "MISSING" not in r.stdout


def test_for_hook_in_sync_is_silent(rig: dict[str, str]) -> None:
    _copy_hook(rig, "read-dedup.py")
    r = _run(rig, "--for-hook")
    assert r.returncode == 0
    assert r.stdout == ""


def test_for_hook_drift_is_one_line(rig: dict[str, str]) -> None:
    _copy_hook(rig, "read-dedup.py", "# stale copy\n")
    r = _run(rig, "--for-hook")
    assert r.returncode == 0
    assert r.stdout == "hook-sync: 1 global hooks stale — run scripts/hook-sync\n"


def test_closed_pipe_is_quiet(rig: dict[str, str]) -> None:
    _copy_hook(rig, "read-dedup.py", "# stale copy\n")
    p = subprocess.run(
        f'bash "{SCRIPT}" | head -1 >/dev/null',
        shell=True,
        env={**os.environ, **rig},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert p.stderr == ""


def test_drifted_copy_reports_the_diff_and_cp(rig: dict[str, str]) -> None:
    g = _copy_hook(rig, "read-dedup.py", "# stale copy\n")
    r = _run(rig)
    assert r.returncode == 0
    assert "read-dedup.py DIFFERS" in r.stdout
    assert f'cp "{REPO_HOOKS}/read-dedup.py" "{g}"' in r.stdout
    # The report shows the diff but never writes the live dir.
    assert "--- " in r.stdout
    assert g.read_text(encoding="utf-8") == "# stale copy\n"


def test_settings_referenced_but_missing_live_is_reported(
    rig: dict[str, str],
) -> None:
    Path(rig["PRECIS_HOOKS_SETTINGS"]).write_text(
        '{"command": "G=\\"$HOME/.claude/hooks/compact-state-note.sh\\""}',
        encoding="utf-8",
    )
    r = _run(rig)
    assert r.returncode == 0
    assert "compact-state-note.sh MISSING" in r.stdout
    assert "cp " in r.stdout


def test_live_only_file_is_flagged_not_synced(rig: dict[str, str]) -> None:
    _live(rig).joinpath("orphan-hook.py").write_text("x = 1\n", encoding="utf-8")
    r = _run(rig)
    assert r.returncode == 0
    assert "orphan-hook.py" in r.stdout
    assert "NO repo counterpart" in r.stdout


def test_sourced_helper_missing_is_reported(rig: dict[str, str]) -> None:
    """A live hook sourcing a helper that was never copied is drift too —
    helpers are sibling files, not settings entries (_devin-detect.sh)."""
    _copy_hook(rig, "read-dedup.py")
    _live(rig).joinpath("consumer.sh").write_text(
        '#!/usr/bin/env bash\n. "$(dirname "$0")/_devin-detect.sh"\n',
        encoding="utf-8",
    )
    # consumer.sh has no repo counterpart either; both must be reported.
    r = _run(rig)
    assert r.returncode == 0
    assert "consumer.sh" in r.stdout and "NO repo counterpart" in r.stdout
    assert "_devin-detect.sh MISSING" in r.stdout


def test_repo_hook_python_import_names_missing_helper(
    rig: dict[str, str], tmp_path: Path
) -> None:
    """gr537787: a repo hook importing a sibling module (`from _x import …`
    — no .py suffix for the live-grep to see) must name the helper when
    it's absent live; copying only the drifted hook would break it."""
    repo = tmp_path / "repo-hooks"
    repo.mkdir()
    hook_src = "import sys\nfrom _pykind_hits import pykind_hits\n"
    (repo / "nudge.py").write_text(hook_src, encoding="utf-8")
    (repo / "_pykind_hits.py").write_text(
        "def pykind_hits():\n    return 1\n", encoding="utf-8"
    )
    rig["PRECIS_HOOKS_REPO"] = str(repo)
    # The live copy of the hook is in sync — only the helper drifts.
    _live(rig).joinpath("nudge.py").write_text(hook_src, encoding="utf-8")
    r = _run(rig)
    assert r.returncode == 0
    assert "nudge.py DIFFERS" not in r.stdout
    assert "_pykind_hits.py MISSING" in r.stdout
    assert "needed by: nudge.py" in r.stdout
    assert f'cp "{repo}/_pykind_hits.py" "{_live(rig)}/_pykind_hits.py"' in r.stdout


def test_helper_import_scan_is_transitive(rig: dict[str, str], tmp_path: Path) -> None:
    """A helper's own sibling import counts too: hook → _a → _b, with
    only _b absent live, names _b and the helper that needs it."""
    repo = tmp_path / "repo-hooks"
    repo.mkdir()
    (repo / "nudge.py").write_text("import _a\n", encoding="utf-8")
    (repo / "_a.py").write_text("from _b import thing\n", encoding="utf-8")
    (repo / "_b.py").write_text("thing = 1\n", encoding="utf-8")
    rig["PRECIS_HOOKS_REPO"] = str(repo)
    live = _live(rig)
    live.joinpath("nudge.py").write_text("import _a\n", encoding="utf-8")
    live.joinpath("_a.py").write_text("from _b import thing\n", encoding="utf-8")
    r = _run(rig)
    assert r.returncode == 0
    assert "_b.py MISSING" in r.stdout
    assert "needed by: _a.py" in r.stdout


def test_drifted_helper_reports_needed_by(rig: dict[str, str], tmp_path: Path) -> None:
    """A stale live helper (not just a missing one) names its importer."""
    repo = tmp_path / "repo-hooks"
    repo.mkdir()
    hook_src = "import _a\n"
    (repo / "nudge.py").write_text(hook_src, encoding="utf-8")
    (repo / "_a.py").write_text("x = 2\n", encoding="utf-8")
    rig["PRECIS_HOOKS_REPO"] = str(repo)
    live = _live(rig)
    live.joinpath("nudge.py").write_text(hook_src, encoding="utf-8")
    live.joinpath("_a.py").write_text("x = 1\n", encoding="utf-8")
    r = _run(rig)
    assert r.returncode == 0
    assert "_a.py DIFFERS" in r.stdout
    assert "needed by: nudge.py" in r.stdout


def test_no_live_dir_is_one_line_not_a_report(
    rig: dict[str, str], tmp_path: Path
) -> None:
    rig["PRECIS_HOOKS_LIVE"] = str(tmp_path / "absent")
    r = _run(rig)
    assert r.returncode == 0
    assert "nothing to compare" in r.stdout
    assert "MISSING" not in r.stdout


def test_usage_error(rig: dict[str, str]) -> None:
    r = _run(rig, "--bogus")
    assert r.returncode == 2
    assert "usage" in r.stderr
