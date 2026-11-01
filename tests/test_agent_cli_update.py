"""Agent CLI report/update door — gr543827.

``deploy/roles/agent_clis/files/agent_cli.py`` runs on each host (stdlib
only) and ``scripts/agent-cli-update`` drives it through the play wrapper.
The host tool is exercised against a fake host (versions dict + fake
installer), so install, smoke, rollback and the pin-driven rollback run
without touching a real CLI.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ROLE = REPO / "deploy" / "roles" / "agent_clis"
WRAPPER = REPO / "scripts" / "agent-cli-update"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tool = _load(ROLE / "files" / "agent_cli.py", "agent_cli_tool")


class FakeHost:
    """Installed versions plus a fake installer and headless prompt."""

    def __init__(self, versions: dict[str, str], bad_prompt: set[str] | None = None):
        self.versions = dict(versions)
        self.bad_prompt = bad_prompt or set()
        self.calls: list[list[str]] = []

    def find(self, name: str) -> str | None:
        return f"/bin/{name}" if name in self.versions else None

    def run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[0] == "npm":
            pkg, _, version = argv[-1].rpartition("@")
            cli = {"@anthropic-ai/claude-code": "claude", "@openai/codex": "codex"}[pkg]
            self.versions[cli] = version
            return 0, "added 1 package"
        if argv[0] == "devin":
            self.versions["devin"] = "9.9.9"
            return 0, "updated"
        cli = Path(argv[0]).name
        if argv[1] == "--version":
            return 0, f"{cli} version {self.versions[cli]}"
        return (1, "") if cli in self.bad_prompt else (0, "OK")


def _config(
    pins: dict[str, str], only: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    defaults = yaml.safe_load(
        (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    clis = {
        c: {**defaults["agent_cli_installers"][c], "pin": pins.get(c, ""), "min": ""}
        for c in tool.CLIS
    }
    return {"clis": clis, "only": only or [], "smoke_prompt": True, **extra}


def test_parse_version_reads_the_first_dotted_number() -> None:
    assert tool.parse_version("2.1.143 (Claude Code)") == "2.1.143"
    assert tool.parse_version("codex-cli 0.46.0") == "0.46.0"
    assert tool.parse_version("kimi, version 2.3.3") == "2.3.3"
    assert tool.parse_version("no digits") is None


@pytest.mark.parametrize(
    ("version", "pin", "minimum", "status"),
    [
        (None, "1.0.0", "", "missing"),
        ("1.0.0", "", "", "unpinned"),
        ("1.0.0", "1.0.0", "", "ok"),
        ("1.9.0", "1.10.0", "", "behind"),
        ("2.0.0", "1.10.0", "", "ahead"),
        ("1.0.0", "1.0.0", "1.2.0", "below-min"),
    ],
)
def test_classify(version: str | None, pin: str, minimum: str, status: str) -> None:
    assert tool.classify(version, pin, minimum) == status


def test_report_is_read_only_and_alerts_on_a_host_behind_its_pin() -> None:
    host = FakeHost({"claude": "2.0.0", "codex": "0.46.0"})
    rows = tool.report(
        _config({"claude": "2.1.0", "codex": "0.46.0"}), host.run, host.find
    )
    by = {r["cli"]: r for r in rows}
    assert by["claude"]["status"] == "behind"
    assert by["codex"]["status"] == "ok"
    assert by["devin"]["status"] == "missing"
    assert all(c[1:] == ["--version"] for c in host.calls)
    alerts = tool.alerts(rows)
    assert any("claude: behind" in a for a in alerts)
    assert not any("devin" in a for a in alerts), "an unpinned absent CLI is no alert"


def test_update_installs_the_pin_and_the_version_changes() -> None:
    host = FakeHost({"claude": "2.0.0"})
    [row] = tool.update(
        _config({"claude": "2.1.0"}, only=["claude"]), host.run, host.find
    )
    assert (row["action"], row["ok"]) == ("updated", True)
    assert (row["previous"], row["version"]) == ("2.0.0", "2.1.0")
    assert host.versions["claude"] == "2.1.0"
    assert ["npm", "install", "-g", "@anthropic-ai/claude-code@2.1.0"] in host.calls


def test_update_is_a_noop_when_already_on_the_pin() -> None:
    host = FakeHost({"claude": "2.1.0"})
    [row] = tool.update(
        _config({"claude": "2.1.0"}, only=["claude"]), host.run, host.find
    )
    assert row["action"] == "current"
    assert not any(c[0] == "npm" for c in host.calls)


def test_failed_smoke_rolls_the_host_back_and_fails_the_run() -> None:
    host = FakeHost({"claude": "2.0.0"}, bad_prompt={"claude"})
    [row] = tool.update(
        _config({"claude": "2.1.0"}, only=["claude"]), host.run, host.find
    )
    assert (row["action"], row["ok"]) == ("rolled-back", False)
    assert "smoke failed" in row["detail"]
    assert host.versions["claude"] == "2.0.0"
    assert tool.alerts([row])[0].startswith("ALERT claude: rolled-back")


def test_no_prompt_skips_the_headless_smoke() -> None:
    host = FakeHost({"claude": "2.0.0"}, bad_prompt={"claude"})
    cfg = _config({"claude": "2.1.0"}, only=["claude"], smoke_prompt=False)
    [row] = tool.update(cfg, host.run, host.find)
    assert row["action"] == "updated"


def test_restoring_the_previous_pin_rolls_back_by_pin() -> None:
    host = FakeHost({"claude": "2.0.0"})
    tool.update(_config({"claude": "2.1.0"}, only=["claude"]), host.run, host.find)
    [row] = tool.update(
        _config({"claude": "2.0.0"}, only=["claude"]), host.run, host.find
    )
    assert (row["action"], row["version"]) == ("updated", "2.0.0")
    assert host.versions["claude"] == "2.0.0"


def test_unpinned_cli_is_skipped_unless_named_then_refused() -> None:
    host = FakeHost({"claude": "2.0.0", "codex": "0.1.0"})
    rows = tool.update(_config({"claude": "2.0.0"}), host.run, host.find)
    by = {r["cli"]: r for r in rows}
    assert by["codex"]["action"] == "skipped" and by["codex"]["ok"]
    named = tool.update(_config({}, only=["codex"]), host.run, host.find)[0]
    assert (named["action"], named["ok"]) == ("refused", False)
    assert "agent_cli_pin_codex" in named["detail"]


def test_unpinnable_installer_fails_without_a_rollback() -> None:
    host = FakeHost({"devin": "1.0.0"})
    [row] = tool.update(
        _config({"devin": "1.5.0"}, only=["devin"]), host.run, host.find
    )
    assert (row["action"], row["ok"]) == ("failed", False)
    assert "no rollback" in row["detail"]


def test_unsafe_pin_never_reaches_the_installer() -> None:
    host = FakeHost({"claude": "2.0.0"})
    [row] = tool.update(
        _config({"claude": "1.0 && rm -rf /"}, only=["claude"]), host.run, host.find
    )
    assert row["ok"] is False
    assert not any(c[0] == "npm" and "rm" in " ".join(c) for c in host.calls)


def test_main_prints_the_json_line_and_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = FakeHost({"claude": "2.0.0"})
    cfg = json.dumps(_config({"claude": "2.1.0"}, only=["claude"]))
    rc = tool.main(["update", "--config-json", cfg], host.run, host.find)
    out = capsys.readouterr().out
    payload = json.loads(out.rsplit("AGENT_CLI_JSON ", 1)[1])
    assert rc == 0 and payload["rows"][0]["version"] == "2.1.0"
    behind = json.dumps(_config({"claude": "3.0.0"}, only=["claude"]))
    assert tool.main(["report", "--config-json", behind], host.run, host.find) == 0
    strict = ["report", "--strict", "--config-json", behind]
    assert tool.main(strict, host.run, host.find) == 1


def _wrapper(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRAPPER), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={"PRECIS_AGENT_CLI_PLAY": "play-stub", "PATH": ""},
    )


def test_wrapper_defaults_to_a_dry_run_report() -> None:
    proc = _wrapper("--host", "h1", "--print")
    assert proc.returncode == 0
    assert "agent_cli_mode=report" in proc.stdout
    assert "--limit h1" in proc.stdout and "30-agent-clis.yml" in proc.stdout


def test_wrapper_apply_names_cli_and_group() -> None:
    proc = _wrapper(
        "--group", "g", "--host", "h", "--cli", "claude,kimi", "--apply", "--print"
    )
    assert "agent_cli_mode=update" in proc.stdout
    assert "--limit h:g" in proc.stdout
    assert "agent_cli_only=claude,kimi" in proc.stdout


def test_wrapper_refuses_the_whole_fleet_and_unknown_clis() -> None:
    assert _wrapper("--print").returncode == 2
    bad = _wrapper("--host", "h", "--cli", "gemini", "--print")
    assert bad.returncode == 2 and "unknown --cli" in bad.stderr


def test_agent_clis_play_is_outside_every_deploy() -> None:
    for name in ("site.yml", "redeploy-precis.yml"):
        text = (REPO / "deploy" / name).read_text(encoding="utf-8")
        assert "30-agent-clis" not in text, f"{name} must not run the CLI updater"
    play = yaml.safe_load(
        (REPO / "deploy" / "playbooks" / "30-agent-clis.yml").read_text(
            encoding="utf-8"
        )
    )
    assert play[0]["roles"] == ["agent_clis"]


def test_role_defaults_ship_unpinned_and_report_only() -> None:
    d = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert d["agent_cli_mode"] == "report"
    assert all(d[f"agent_cli_pin_{c}"] == "" for c in tool.CLIS)
    assert set(d["agent_cli_installers"]) == set(tool.CLIS)
