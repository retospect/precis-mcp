"""Real throwaway Git trees; fake tmux/Codex cannot affect live sessions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet-codex"
THREAD = "01900000-0000-7000-8000-000000000001"
# Codex 0.161 runs embedded (code mode, hence MCP, broken) under any of these.
EMBEDDED_MODE_FLAGS = {
    "--no-daemon",
    "--approve-for-me",
    "--profile",
    "-m",
    "-c",
    "--enable",
    "--disable",
}
OTHER_THREAD = "01900000-0000-7000-8000-000000000002"

FAKE_COMMAND = r"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

state = Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
name = Path(sys.argv[0]).name
with (state / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([name, args]) + "\n")
if name == "codex":
    expected_file = os.environ.get("FAKE_EXPECTED_TOKEN_FILE")
    if expected_file:
        expected = Path(expected_file).read_text(encoding="utf-8").strip()
        with (state / "codex-env.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"token_matches": os.environ.get("PRECIS_MCP_TOKEN") == expected}) + "\n")
    print("queued")
    raise SystemExit(0)
windows = json.loads((state / "windows.json").read_text(encoding="utf-8"))
if args[0] == "list-windows":
    for index, window in windows.items():
        print("\t".join([index, window["name"], window["window_id"], window["pane_id"], "1" if window.get("dead") else "0"]))
elif args[0] == "new-window":
    index = args[args.index("-t") + 1].rsplit(":", 1)[1]
    if index in windows:
        raise SystemExit(2)
    window = {
        "name": args[args.index("-n") + 1],
        "window_id": "@" + index,
        "pane_id": "%" + index,
    }
    windows[index] = window
    (state / "windows.json").write_text(json.dumps(windows), encoding="utf-8")
    # Execute the shell command against fake Codex to prove argument round-tripping.
    result = subprocess.run(["/bin/sh", "-c", args[-1]], capture_output=True, text=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    print(window["window_id"] + "\t" + window["pane_id"])
else:
    raise SystemExit("forbidden tmux operation: " + args[0])
"""


@dataclass
class Fleet:
    root: Path
    state: Path
    fake: Path
    env: dict[str, str]
    roster: Path

    def run(self, *args: str, cwd: Path | None = None, **env: str):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--roster",
                str(self.roster),
                "--state",
                str(self.state),
                "--root",
                str(self.root),
                "--session",
                "0",
                *args,
            ],
            cwd=cwd or self.root,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=cwd or self.root, text=True, stderr=subprocess.DEVNULL
        ).strip()

    def windows(self) -> dict:
        return json.loads((self.fake / "windows.json").read_text(encoding="utf-8"))

    def set_windows(self, windows: dict) -> None:
        (self.fake / "windows.json").write_text(json.dumps(windows), encoding="utf-8")

    def calls(self, command: str) -> list[list[str]]:
        path = self.fake / "calls.jsonl"
        if not path.exists():
            return []
        return [
            args
            for name, args in map(
                json.loads, path.read_text(encoding="utf-8").splitlines()
            )
            if name == command
        ]

    def tree(self, name: str) -> Path:
        return self.root / ".claude" / "worktrees" / f"codex-{name}"

    def registration(self, name: str = "graph-memory", thread: str = THREAD):
        result = self.run(
            "register",
            name,
            cwd=self.tree(name),
            CODEX_THREAD_ID=thread,
            TMUX_PANE=f"%{2 if name == 'graph-memory' else 3}",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


@pytest.fixture
def fleet():
    # Use an isolated worktree scratch directory, never shared /tmp scripts.
    scratch_parent = ROOT / ".claude"
    scratch_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="fleet-codex-test-", dir=scratch_parent
    ) as scratch:
        root = Path(scratch) / "repo with 'quotes' and $dollars"
        root.mkdir()
        state = Path(scratch) / "shared state"
        (state / "prompts").mkdir(parents=True)
        fake = Path(scratch) / "fake"
        fake.mkdir()
        (fake / "windows.json").write_text(
            json.dumps(
                {
                    "0": {"name": "coordinator", "window_id": "@0", "pane_id": "%0"},
                }
            ),
            encoding="utf-8",
        )
        for command in ("tmux", "codex"):
            path = fake / command
            path.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND, encoding="utf-8")
            path.chmod(0o755)
        roster = state / "roster.tsv"
        roster.write_text(
            "index\tname\tmodel\teffort\tthreads\tsummary\n"
            "0\tcoordinator\tgpt-6-astra\thigh\tall\tCoordinate\n"
            "2\tgraph-memory\tgpt-6-astra\thigh\tmemory\tBuild graph memory\n"
            "3\tknowledge-mesh\tgpt-6.1-sol\tmedium\tmesh\tBuild mesh\n",
            encoding="utf-8",
        )
        for name in ("graph-memory", "knowledge-mesh"):
            (state / "prompts" / f"{name}.txt").write_text(
                "Read AGENTS.md. Literal 'quotes', $HOME, `uname`, $(pwd) and\nnewlines.",
                encoding="utf-8",
            )
        codex_home = fake / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PATH": str(fake) + os.pathsep + os.environ["PATH"],
            "FAKE_STATE": str(fake),
            "CODEX_HOME": str(codex_home),
        }
        value = Fleet(root, state, fake, env, roster)
        value.git("init", "-b", "main")
        (root / "README.md").write_text("initial\n", encoding="utf-8")
        value.git("add", "README.md")
        value.git(
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "initial",
        )
        yield value


def test_isolated_branches_idempotence_dirty_preservation_and_quoting(fleet: Fleet):
    result = fleet.run("up")
    assert result.returncode == 0, result.stderr
    assert set(fleet.windows()) == {"0", "2", "3"}
    assert fleet.git("branch", "--show-current") == "main"
    first_head = fleet.git("rev-parse", "main")
    for name in ("graph-memory", "knowledge-mesh"):
        assert (
            fleet.git("branch", "--show-current", cwd=fleet.tree(name))
            == f"work/{name}/bootstrap"
        )
        assert fleet.git("rev-parse", "HEAD", cwd=fleet.tree(name)) == first_head
        assert (fleet.state / "workers" / f"{name}.json").exists()
    command = fleet.calls("codex")[0]
    assert command[command.index("-C") + 1] == str(fleet.tree("graph-memory"))
    assert command[-1] == (fleet.state / "prompts" / "graph-memory.txt").read_text(
        encoding="utf-8"
    )
    assert command[0] == "-C"
    assert set(command) & EMBEDDED_MODE_FLAGS == set()
    assert command[command.index("--add-dir") + 1] == str(fleet.state)
    assert "knowledge-mesh: config model gpt-6-astra != roster gpt-6.1-sol" in (
        result.stderr
    )
    assert "graph-memory:" not in result.stderr
    dirty = fleet.tree("graph-memory") / "README.md"
    dirty.write_text("worker's unfinished work\n", encoding="utf-8")
    rerun = fleet.run("up")
    assert rerun.returncode == 0, rerun.stderr
    assert dirty.read_text(encoding="utf-8") == "worker's unfinished work\n"
    assert len(fleet.calls("codex")) == 2
    assert len([args for args in fleet.calls("tmux") if args[0] == "new-window"]) == 2


@pytest.mark.parametrize("name", ["other-owner", "graph-memory"])
def test_occupied_unmanaged_window_is_preserved(fleet: Fleet, name: str):
    windows = fleet.windows()
    windows["2"] = {"name": name, "window_id": "@old", "pane_id": "%old"}
    fleet.set_windows(windows)
    result = fleet.run("up")
    assert result.returncode == 2
    assert "occupied window" in result.stderr
    assert fleet.windows() == windows
    assert not fleet.tree("knowledge-mesh").exists()
    assert not fleet.calls("codex")


def test_dry_run_has_no_filesystem_or_tmux_mutations(fleet: Fleet):
    before = sorted(str(path) for path in fleet.state.rglob("*"))
    result = fleet.run("--dry-run", "up", "graph-memory")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "would-start"
    assert before == sorted(str(path) for path in fleet.state.rglob("*"))
    assert not fleet.tree("graph-memory").exists()
    assert not fleet.git("branch", "--list", "work/graph-memory/bootstrap")
    assert all(args[0] == "list-windows" for args in fleet.calls("tmux"))
    assert not fleet.calls("codex")


def test_register_native_queue_and_status(fleet: Fleet):
    assert fleet.run("up", "graph-memory").returncode == 0
    record = fleet.registration()
    assert record["thread_id"] == THREAD
    assert record["pane_id"] == "%2"
    message = "Deploy abc123; dogfood $(no-shell) `no-shell` 'quotes'\nsecond line"
    result = fleet.run("send", "graph-memory", "--message", message)
    assert result.returncode == 0, result.stderr
    assert fleet.calls("codex")[-1] == [
        "queue",
        "--thread",
        THREAD,
        "--message",
        message,
    ]
    result = fleet.run("status", "graph-memory")
    assert json.loads(result.stdout)["status"] == "registered"
    assert json.loads(result.stdout)["window"]["pane_id"] == "%2"
    assert all(
        args[0] in {"list-windows", "new-window"} for args in fleet.calls("tmux")
    )


def test_send_refuses_missing_stale_and_duplicate_registrations(fleet: Fleet):
    assert fleet.run("up").returncode == 0
    missing = fleet.run("send", "graph-memory", "--message", "hello")
    assert missing.returncode == 2 and "missing registered" in missing.stderr
    record = fleet.registration()
    fleet.registration("knowledge-mesh", OTHER_THREAD)
    windows = fleet.windows()
    windows["2"]["pane_id"] = "%replacement"
    fleet.set_windows(windows)
    stale = fleet.run("send", "graph-memory", "--message", "hello")
    assert stale.returncode == 2 and "pane_id changed" in stale.stderr
    windows["2"]["pane_id"] = "%2"
    fleet.set_windows(windows)
    record["name"] = "knowledge-mesh"
    (fleet.state / "registrations" / "knowledge-mesh.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    ambiguous = fleet.run("send", "graph-memory", "--message", "hello")
    assert ambiguous.returncode == 2 and "ambiguous" in ambiguous.stderr
    assert all(args[0] != "queue" for args in fleet.calls("codex"))


def test_register_requires_correct_pane_worktree_and_unique_uuid(fleet: Fleet):
    assert fleet.run("up").returncode == 0
    wrong_tree = fleet.run(
        "register", "graph-memory", "--thread", THREAD, TMUX_PANE="%2"
    )
    assert wrong_tree.returncode == 2 and "its worktree" in wrong_tree.stderr
    # A daemon-run tool command carries the daemon's pane, not the worker's;
    # the owned window and worktree identify the worker instead.
    daemon_pane = fleet.run(
        "register",
        "graph-memory",
        "--thread",
        THREAD,
        cwd=fleet.tree("graph-memory"),
        TMUX_PANE="%0",
    )
    assert daemon_pane.returncode == 0, daemon_pane.stderr
    assert json.loads(daemon_pane.stdout)["pane_id"] == "%2"
    windows = fleet.windows()
    windows["2"]["dead"] = True
    fleet.set_windows(windows)
    dead = fleet.run(
        "register", "graph-memory", "--thread", THREAD, cwd=fleet.tree("graph-memory")
    )
    assert dead.returncode == 2
    windows["2"]["dead"] = False
    fleet.set_windows(windows)
    fleet.registration()
    duplicate = fleet.run(
        "register",
        "knowledge-mesh",
        "--thread",
        THREAD,
        cwd=fleet.tree("knowledge-mesh"),
        TMUX_PANE="%3",
    )
    assert duplicate.returncode == 2 and "already registered" in duplicate.stderr


def test_worktree_branch_mismatch_is_preserved(fleet: Fleet):
    assert fleet.run("up", "graph-memory").returncode == 0
    fleet.git("switch", "-c", "worker-progress", cwd=fleet.tree("graph-memory"))
    result = fleet.run("up", "graph-memory")
    assert result.returncode == 2 and "worktree identity differs" in result.stderr
    assert (
        fleet.git("branch", "--show-current", cwd=fleet.tree("graph-memory"))
        == "worker-progress"
    )
    assert len(fleet.calls("codex")) == 1


def test_restarts_missing_window_without_resetting_existing_worktree(fleet: Fleet):
    assert fleet.run("up", "graph-memory").returncode == 0
    dirty = fleet.tree("graph-memory") / "README.md"
    dirty.write_text("preserved\n", encoding="utf-8")
    windows = fleet.windows()
    del windows["2"]
    fleet.set_windows(windows)
    result = fleet.run("up", "graph-memory")
    assert result.returncode == 0, result.stderr
    assert dirty.read_text(encoding="utf-8") == "preserved\n"


def test_credential_bootstrap_uses_only_child_environment(fleet: Fleet):
    token_file = fleet.fake / "token-input"
    synthetic_token = "synthetic-test-credential-only"
    token_file.write_text(synthetic_token + "\n", encoding="utf-8")
    result = fleet.run(
        "--token-file",
        str(token_file),
        "up",
        "graph-memory",
        PRECIS_MCP_TOKEN="stale-test-value",
        FAKE_EXPECTED_TOKEN_FILE=str(token_file),
    )
    assert result.returncode == 0, result.stderr
    environment_check = (fleet.fake / "codex-env.jsonl").read_text(encoding="utf-8")
    assert json.loads(environment_check)["token_matches"]
    assert synthetic_token not in result.stdout + result.stderr
    for path in [fleet.fake / "calls.jsonl", *fleet.state.rglob("*.json")]:
        assert synthetic_token not in path.read_text(encoding="utf-8")
    command = fleet.calls("codex")[0]
    assert set(command) & EMBEDDED_MODE_FLAGS == set()
    assert str(token_file) not in command


@pytest.mark.parametrize("contents", [None, b"", b"invalid\x00token", b"\xff\xfe"])
def test_bad_configured_token_file_fails_before_start(
    fleet: Fleet, contents: bytes | None
):
    token_file = fleet.fake / "token-input"
    if contents is not None:
        token_file.write_bytes(contents)
    result = fleet.run("--token-file", str(token_file), "up", "graph-memory")
    assert result.returncode == 2
    assert "token file" in result.stderr
    assert set(fleet.windows()) == {"0"}
    assert not fleet.tree("graph-memory").exists()
    assert not fleet.calls("codex")
    assert all(args[0] == "list-windows" for args in fleet.calls("tmux"))


def test_missing_window_resumes_saved_thread_and_preserves_notes(fleet: Fleet):
    assert fleet.run("up", "graph-memory").returncode == 0
    registration = fleet.registration()
    registration["handoff"] = "Review graph evidence before the next slice."
    registration_file = fleet.state / "registrations" / "graph-memory.json"
    registration_file.write_text(json.dumps(registration), encoding="utf-8")
    notes = fleet.state / "graph-memory-notes.md"
    notes.write_text("Keep this unfinished hypothesis.\n", encoding="utf-8")
    windows = fleet.windows()
    del windows["2"]
    fleet.set_windows(windows)
    result = fleet.run("up", "graph-memory")
    assert result.returncode == 0, result.stderr
    command = fleet.calls("codex")[-1]
    assert command[:2] == ["resume", THREAD]
    assert set(command) & EMBEDDED_MODE_FLAGS == set()
    assert json.loads(registration_file.read_text(encoding="utf-8")) == registration
    assert notes.read_text(encoding="utf-8") == "Keep this unfinished hypothesis.\n"
    worker = json.loads(
        (fleet.state / "workers" / "graph-memory.json").read_text(encoding="utf-8")
    )
    assert worker["resumed_thread_id"] == THREAD


def test_resume_refuses_ambiguous_saved_identity(fleet: Fleet):
    assert fleet.run("up", "graph-memory").returncode == 0
    registration = fleet.registration()
    (fleet.state / "registrations" / "copy.json").write_text(
        json.dumps(registration), encoding="utf-8"
    )
    windows = fleet.windows()
    del windows["2"]
    fleet.set_windows(windows)
    result = fleet.run("up", "graph-memory")
    assert result.returncode == 2 and "ambiguous" in result.stderr
    assert fleet.windows() == windows
    assert len(fleet.calls("codex")) == 1


def test_unknown_name_and_invalid_roster_refuse_before_mutations(fleet: Fleet):
    result = fleet.run("up", "nonexistent")
    assert result.returncode == 2 and "unknown workers" in result.stderr
    with fleet.roster.open("a", encoding="utf-8") as stream:
        stream.write("2\tanother\tgpt-6.1-sol\thigh\ta\tduplicate index\n")
    result = fleet.run("up")
    assert result.returncode == 2 and "duplicate" in result.stderr
    assert not fleet.calls("codex")


def test_coordinator_registration_can_be_seeded_without_worker_record(fleet: Fleet):
    registrations = fleet.state / "registrations"
    registrations.mkdir()
    (registrations / "coordinator.json").write_text(
        json.dumps(
            {
                "name": "coordinator",
                "thread_id": THREAD,
                "pane_id": "%0",
                "worktree": "/coordinator/explicit/worktree",
            }
        ),
        encoding="utf-8",
    )
    result = fleet.run("send", "coordinator", "--message", "review requested")
    assert result.returncode == 0, result.stderr
    assert fleet.calls("codex")[-1][0] == "queue"
