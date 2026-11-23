"""The agent-host probe publishes only the tracked working-tree state."""

from __future__ import annotations

import plistlib
import shlex
import subprocess
from pathlib import Path

import jinja2

_TEMPLATES = (
    Path(__file__).resolve().parents[1] / "deploy/roles/node_exporter/templates"
)


def test_probe_reports_tracked_edits_and_failed_checks(tmp_path: Path) -> None:
    repo = tmp_path / "git deploy helper"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    tracked = repo / "settings.yml"
    tracked.write_text("original", encoding="utf-8")
    subprocess.run([*git, "add", tracked.name], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Initial settings",
        ],
        check=True,
    )

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["quote"] = shlex.quote
    template = (_TEMPLATES / "deploy-overlay-metrics.sh.j2").read_text(encoding="utf-8")
    template = template.replace('/usr/bin/sudo -H -u "$owner" /usr/bin/git', "git")
    template = template.replace("/private/var/run", shlex.quote(str(tmp_path)))
    # Exercise the same rename on Linux, which spells BSD mv's -h as -T.
    template = template.replace("mv -fh", "mv -f")
    script = env.from_string(template).render(
        _overlay_helpers={"files": [{"path": str(repo)}]},
        _overlay_helper={"stat": {"pw_name": "operator"}},
        node_exporter_textfile_dir=str(tmp_path),
    )

    def assert_status(expected: int) -> None:
        run = subprocess.run(["sh", "-c", script], check=True, capture_output=True)
        assert run.stdout == run.stderr == b""
        assert (tmp_path / "deploy_overlay.prom").read_text(encoding="utf-8") == (
            f"precis_deploy_overlay_status {expected}\n"
        )

    assert_status(0)
    (repo / "settings.yml.bak").write_text("ignored", encoding="utf-8")
    assert_status(0)
    tracked.write_text("local edit", encoding="utf-8")
    assert_status(1)
    subprocess.run([*git, "add", tracked.name], check=True)
    assert_status(1)
    subprocess.run([*git, "reset", "--hard", "-q", "HEAD"], check=True)
    assert_status(0)
    tracked.unlink()
    assert_status(1)
    (repo / ".git").rename(repo / ".missing-git")
    assert_status(-1)


def test_launchd_checks_on_load_and_every_five_minutes() -> None:
    unit = plistlib.loads(
        (_TEMPLATES / "com.precis.deploy-overlay-metrics.plist.j2").read_bytes()
    )
    assert unit["RunAtLoad"] is True
    assert unit["StartInterval"] == 300
    assert unit["ProgramArguments"] == ["/usr/local/bin/deploy-overlay-metrics"]
