"""scripts/fleet-brief — the wave's census + brief generator (gr515822).

Offline stages (``census``/``briefs``) run against fixture files in
tmp dirs; ``bodies`` is exercised against a fake ``prod-psql`` that
logs its argv and serves canned SQL output — the test also pins that
prod is only ever reached through ``--ro``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fleet-brief"


@pytest.fixture(name="fb")
def _fb() -> ModuleType:
    return load_script("fleet_brief", SCRIPT, register=True)


BATCHES = """\
# name|gripes|key files|lane|notes
alpha|111 222|src/precis/x.py|checked|alpha notes
beta|333|scripts/ship|remote|
written|BRIEF-WRITTEN|||
"""

BODIES = """\
### gr111: first gripe
body one
-- comment: ruling one
### gr222: second gripe
body two
### gr333: third gripe
body three
"""


def _batches(tmp_path: Path) -> Path:
    p = tmp_path / "batches.txt"
    p.write_text(BATCHES, encoding="utf-8")
    return p


def _bodies(tmp_path: Path) -> Path:
    p = tmp_path / "bodies.md"
    p.write_text(BODIES, encoding="utf-8")
    return p


def test_parse_batches_skips_comments_and_written(
    fb: ModuleType, tmp_path: Path
) -> None:
    rows = fb._parse_batches(_batches(tmp_path))
    assert [b.name for b in rows] == ["alpha", "beta"]
    assert rows[0].ids == [111, 222]
    assert rows[0].lane == "checked"
    assert rows[1].lane == "remote"


def test_parse_batches_bad_lane_and_ids(fb: ModuleType, tmp_path: Path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("x|1 2||sideways|\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="lane 'sideways'"):
        fb._parse_batches(p)
    p.write_text("x|abc||| \n", encoding="utf-8")
    with pytest.raises(SystemExit, match="bad gripe ids"):
        fb._parse_batches(p)


def test_census_writes_batch_files(
    fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    rc = fb.main(
        [
            "census",
            "--batches",
            str(_batches(tmp_path)),
            "--bodies",
            str(_bodies(tmp_path)),
            "--state",
            str(state),
        ]
    )
    assert rc == 0
    alpha = (state / "census" / "batch-alpha.md").read_text(encoding="utf-8")
    assert "### gr111: first gripe" in alpha
    assert "### gr222: second gripe" in alpha
    assert "body one" in alpha and "ruling one" in alpha
    beta = (state / "census" / "batch-beta.md").read_text(encoding="utf-8")
    assert "### gr333" in beta and "### gr111" not in beta
    assert not (state / "census" / "batch-written.md").exists()
    assert "batch-alpha: 2 sections" in capsys.readouterr().out


def test_census_marks_missing_ids(
    fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "b.txt"
    p.write_text("gap|111 999||quick|\n", encoding="utf-8")
    rc = fb.main(
        [
            "census",
            "--batches",
            str(p),
            "--bodies",
            str(_bodies(tmp_path)),
            "--state",
            str(tmp_path / "state"),
        ]
    )
    assert rc == 1
    out = (tmp_path / "state" / "census" / "batch-gap.md").read_text(encoding="utf-8")
    assert "### gr999: MISSING" in out
    assert "missing [999]" in capsys.readouterr().out


def test_briefs_emit_and_skip_existing(
    fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    argv = ["briefs", "--batches", str(_batches(tmp_path)), "--state", str(state)]
    assert fb.main(argv) == 0
    brief = (state / "briefs" / "alpha.md").read_text(encoding="utf-8")
    assert "gr111" in brief and "gr222" in brief
    assert "--checked" in brief
    assert "alpha notes" in brief
    assert "src/precis/x.py" in brief
    assert "handoff-alpha.md" in brief
    assert str(state / "census" / "batch-alpha.md") in brief
    assert "Tier R: commit only" in brief
    # existing brief is never overwritten without --force
    (state / "briefs" / "alpha.md").write_text("HAND EDIT", encoding="utf-8")
    assert fb.main(argv) == 0
    assert "HAND EDIT" in (state / "briefs" / "alpha.md").read_text(encoding="utf-8")
    assert "skip" in capsys.readouterr().out
    assert fb.main([*argv, "--force"]) == 0
    assert "HAND EDIT" not in (state / "briefs" / "alpha.md").read_text(
        encoding="utf-8"
    )


def test_briefs_goal_and_friction(fb: ModuleType, tmp_path: Path) -> None:
    state = tmp_path / "state"
    fr = tmp_path / "friction.md"
    fr.write_text("FRICTION goes here", encoding="utf-8")
    goal = tmp_path / "goal.txt"
    goal.write_text("fix the two things", encoding="utf-8")
    assert (
        fb.main(
            [
                "briefs",
                "--batches",
                str(_batches(tmp_path)),
                "--state",
                str(state),
                "--friction",
                str(fr),
                "--goal",
                f"@{goal}",
            ]
        )
        == 0
    )
    brief = (state / "briefs" / "beta.md").read_text(encoding="utf-8")
    assert "Goal: fix the two things" in brief
    assert "--remote" in brief
    assert "FRICTION goes here" in brief


def test_state_dir_env_default(
    fb: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "envstate"))
    args = fb.parser().parse_args(["census", "--batches", "x"])
    assert fb._state_dir(args) == tmp_path / "envstate"
    monkeypatch.delenv("FLEET_STATE")
    args = fb.parser().parse_args(["census", "--batches", "x", "--state", "s"])
    assert fb._state_dir(args) == Path("s")


def test_bodies_uses_prod_psql_ro_only(fb: ModuleType, tmp_path: Path) -> None:
    """``bodies`` reaches prod through ``prod-psql --ro`` — a fake psql
    records argv (it must see exactly ``--ro``) and serves the dump."""
    fake = tmp_path / "fake-psql"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "open(os.environ['FAKE_LOG'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "sql = sys.stdin.read()\n"
        "assert '--ro' == sys.argv[1]\n"
        "sys.stdout.write('### gr111: first gripe\\nbody one\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    log = tmp_path / "psql-argv.json"
    state = tmp_path / "state"
    os.environ["FAKE_LOG"] = str(log)
    try:
        rc = fb.main(
            [
                "bodies",
                "--batches",
                str(_batches(tmp_path)),
                "--state",
                str(state),
                "--psql",
                str(fake),
            ]
        )
    finally:
        del os.environ["FAKE_LOG"]
    assert rc == 0
    assert fake.read_text(encoding="utf-8")  # exec'd, not silently skipped
    assert __import__("json").loads(log.read_text(encoding="utf-8")) == ["--ro"]
    bodies = (state / "census" / "bodies.md").read_text(encoding="utf-8")
    assert "### gr111: first gripe" in bodies


def test_bodies_propagates_psql_failure(fb: ModuleType, tmp_path: Path) -> None:
    fake = tmp_path / "fake-psql"
    fake.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(SystemExit, match="prod-psql --ro failed"):
        fb.main(
            [
                "bodies",
                "--batches",
                str(_batches(tmp_path)),
                "--state",
                str(tmp_path / "state"),
                "--psql",
                str(fake),
            ]
        )
