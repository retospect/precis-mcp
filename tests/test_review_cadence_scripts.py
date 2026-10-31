"""The seven review-cadence scripts keep their clock in ~/.cache/precis, not in
the tracked runbook (Reto 2026-10-30: dated cadence logs are temporary and do
not belong in the knowledge mesh; an append into docs/runbooks also dirtied
every checkout the pass ran in).

`scripts/<name> --stamp "<note>"` appends `**YYYY-MM-DD** — <note>` to
`$HOME/.cache/precis/<name>.log`; the DUE/ok decision reads the newest dated
line there. Each script is copied with `scripts/cadence-lib.sh` (and a stub
`prod-psql`) into a throwaway repo layout and run with HOME pointed at a temp
dir, so no real cache file or prod connection is touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs bash cadence scripts"
)

REPO = Path(__file__).resolve().parent.parent

# script -> (window days, runbook, noun the ok/DUE line uses)
SCRIPTS: dict[str, tuple[int, str, str]] = {
    "token-review": (7, "docs/runbooks/token-review.md", "pass"),
    "db-thrash-review": (14, "docs/runbooks/db-thrash-review.md", "pass"),
    "gripe-gc-review": (7, "docs/runbooks/gripe-gc.md", "pass"),
    "secrets-check-review": (7, "docs/runbooks/secrets-check.md", "check"),
    "surface-review": (14, "docs/runbooks/surface-review.md", "pass"),
    "fda-grant-review": (30, "docs/runbooks/fda-grant-review.md", "pass"),
    "skill-search-review": (30, "docs/runbooks/skill-search-review.md", "pass"),
}


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A throwaway repo layout: scripts/{the seven, cadence-lib.sh, prod-psql}."""
    scripts = tmp_path / "repo" / "scripts"
    scripts.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy2(REPO / "scripts" / name, scripts / name)
    shutil.copy2(REPO / "scripts" / "cadence-lib.sh", scripts / "cadence-lib.sh")
    stub = scripts / "prod-psql"
    stub.write_text('#!/usr/bin/env bash\necho "${STUB_COUNT:-3}"\n', encoding="utf-8")
    stub.chmod(0o755)
    return tmp_path / "repo"


def _run(
    tree: Path, home: Path, name: str, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "HOME": str(home), **env}
    return subprocess.run(
        [str(tree / "scripts" / name), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        timeout=60,
        check=False,
    )


def _log(home: Path, name: str) -> Path:
    return home / ".cache" / "precis" / f"{name}.log"


@pytest.mark.parametrize("name", SCRIPTS)
def test_stamp_appends_a_dated_line_to_the_cache_log(
    tree: Path, tmp_path: Path, name: str
) -> None:
    home = tmp_path / "home"  # does not exist yet: --stamp must mkdir -p
    res = _run(tree, home, name, "--stamp", "first note")
    line = f"**{_today()}** — first note"
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == line
    assert _log(home, name).read_text(encoding="utf-8") == line + "\n"

    _run(tree, home, name, "--stamp", "second", "note")  # words join
    assert _log(home, name).read_text(encoding="utf-8").splitlines() == [
        line,
        f"**{_today()}** — second note",
    ]


@pytest.mark.parametrize("name", SCRIPTS)
def test_stamp_without_a_note_refuses_and_writes_nothing(
    tree: Path, tmp_path: Path, name: str
) -> None:
    home = tmp_path / "home"
    res = _run(tree, home, name, "--stamp")
    assert res.returncode == 2
    assert f"scripts/{name} --stamp" in res.stderr
    assert not _log(home, name).exists()


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_cache_log_is_due_and_names_the_stamp_command(
    tree: Path, tmp_path: Path, name: str
) -> None:
    res = _run(tree, tmp_path / "home", name)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith(f"{name}: DUE"), res.stdout
    assert f'scripts/{name} --stamp "<note>"' in res.stdout, res.stdout
    assert "append a dated line" not in res.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_newest_line_decides_regardless_of_order_and_markup(
    tree: Path, tmp_path: Path, name: str
) -> None:
    window, _runbook, noun = SCRIPTS[name]
    home = tmp_path / "home"
    log = _log(home, name)
    log.parent.mkdir(parents=True)
    # oldest-first (stamp appends), plain and bold prefixes, a mid-prose date
    # that must not count, and the newest stamp last.
    log.write_text(
        "**2020-01-01** — ancient\n"
        "- 2021-06-01 — plain bullet\n"
        "note: see 2030-01-01 for details\n"
        f"**{_days_ago(1)}** — recent\n",
        encoding="utf-8",
    )
    res = _run(tree, home, name)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith(f"{name}: ok (last {noun} {_days_ago(1)}, 1d ago;"), (
        res.stdout
    )
    assert f"due in {window - 1}d" in res.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_window_boundary_ok_at_the_window_due_one_day_past(
    tree: Path, tmp_path: Path, name: str
) -> None:
    window, _runbook, noun = SCRIPTS[name]
    home = tmp_path / "home"
    log = _log(home, name)
    log.parent.mkdir(parents=True)

    log.write_text(f"**{_days_ago(window)}** — edge\n", encoding="utf-8")
    ok = _run(tree, home, name)
    assert ok.stdout.startswith(f"{name}: ok (last {noun} {_days_ago(window)}"), (
        ok.stdout
    )

    log.write_text(f"**{_days_ago(window + 1)}** — stale\n", encoding="utf-8")
    due = _run(tree, home, name)
    assert due.stdout.startswith(f"{name}: DUE (last {noun} {_days_ago(window + 1)}"), (
        due.stdout
    )
    assert f"> {window}d" in due.stdout
    assert f'scripts/{name} --stamp "<note>"' in due.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_stamp_resets_the_clock(tree: Path, tmp_path: Path, name: str) -> None:
    home = tmp_path / "home"
    assert _run(tree, home, name).stdout.startswith(f"{name}: DUE")
    _run(tree, home, name, "--stamp", "done")
    res = _run(tree, home, name)
    assert res.stdout.startswith(f"{name}: ok (last "), res.stdout
    assert f"{_today()}, 0d ago" in res.stdout


def test_gripe_gc_due_adds_the_best_effort_prod_count(
    tree: Path, tmp_path: Path
) -> None:
    res = _run(tree, tmp_path / "home", "gripe-gc-review", STUB_COUNT="7")
    assert "(7 shipped-marked open gripe(s) in prod, at last check)" in res.stdout


def test_fda_due_adds_the_open_alert_count(tree: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    locked = _run(tree, home, "fda-grant-review", STUB_COUNT="2")
    assert "2 OPEN nas-denied alert(s)" in locked.stdout
    clear = _run(tree, home, "fda-grant-review", STUB_COUNT="0")
    assert "(0 open nas-denied alerts" in clear.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_tracked_runbook_holds_no_dated_log(name: str) -> None:
    """The clock moved out of the mesh: no `## Log` carrying `**YYYY-MM-DD**`
    entries may come back, and the runbook must name the cache file."""
    _window, runbook, _noun = SCRIPTS[name]
    text = (REPO / runbook).read_text(encoding="utf-8")
    assert f"~/.cache/precis/{name}.log" in text
    log = text.split("\n## Log", 1)[-1] if "\n## Log" in text else ""
    assert not re.search(r"^\s*(?:[-*]\s*)?\*\*\d{4}-\d{2}-\d{2}", log, re.M), (
        f"{runbook} grew a dated log again; use scripts/{name} --stamp"
    )
