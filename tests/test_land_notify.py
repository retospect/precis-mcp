"""scripts/lib/land_notify.py — scripts/ship's post-land status writes.

gr541800 / td541978: after the CAS push, the handles ending the squash
subject move — each gripe gets ``resolved: <sha> (main, release pending)``
+ STATUS:verify in one put, each todo a ``landed: <sha>`` comment then
STATUS:done. Pinned here: the subject parser, the exact prod-precis argv,
and that a refused or crashing prod call never fails (exit 0, one line).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "lib" / "land_notify.py"
SHA = "0123456789abcdef0123456789abcdef01234567"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs a fake CLI"
)


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    return load_script("land_notify", SCRIPT)


@pytest.mark.parametrize(
    ("subject", "want"),
    [
        ("feat(x): y (gr12)", [("gripe", 12)]),
        ("feat(x): y (gr1, td2)", [("gripe", 1), ("todo", 2)]),
        ("feat(x): y (td9 gr8 td9)", [("todo", 9), ("gripe", 8)]),
        ("feat(x): y (gr541800, td541978)  ", [("gripe", 541800), ("todo", 541978)]),
        # Mentions that are not the trailing handle list never match.
        ("feat(x): gr12 half done", []),
        ("feat(x): y (gr12) and more", []),
        ("feat(x): y (see gr12)", []),
        ("feat(x): y", []),
    ],
)
def test_parse_handles(
    mod: ModuleType, subject: str, want: list[tuple[str, int]]
) -> None:
    assert mod.parse_handles(subject) == want


def test_payloads_gripe_then_todo(mod: ModuleType) -> None:
    subject = "feat(gripe): ladder (gr7, td8)"
    plan = mod.payloads(SHA, subject)
    assert [label for label, _ in plan] == ["gr7 auto_verify", "td8 done"]
    (gripe_calls,) = [calls for label, calls in plan if label.startswith("gr7")]
    assert gripe_calls == [
        [
            "tools", "put", "--kind", "gripe", "--id", "7",
            "--text",
            f"resolved: {SHA[:9]} (main, release pending) — landed: {subject}",
            "--tags", json.dumps(["STATUS:auto_verify"]),
        ]
    ]  # fmt: skip

    (todo_calls,) = [calls for label, calls in plan if label.startswith("td8")]
    assert todo_calls == [
        ["tools", "put", "--kind", "todo", "--id", "8",
         "--text", f"landed: {SHA[:9]} — {subject}"],
        ["tools", "tag", "--kind", "todo", "--id", "8",
         "--add", json.dumps(["STATUS:done"])],
    ]  # fmt: skip


def test_payloads_user_verify_kind(mod: ModuleType) -> None:
    plan = mod.payloads(SHA, "feat(x): y (gr7)", verify_kind="user")
    (gripe_calls,) = [calls for _label, calls in plan]
    assert json.loads(gripe_calls[0][gripe_calls[0].index("--tags") + 1]) == [
        "STATUS:user_verify"
    ]
    assert plan[0][0] == "gr7 user_verify"


def test_notify_reports_ok_and_failed_in_one_line(mod: ModuleType) -> None:
    seen: list[list[str]] = []

    def run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        if cmd[cmd.index("--id") + 1] == "8":
            return subprocess.CompletedProcess(cmd, 3, "", "[error:BadInput] nope")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    line = mod.notify(SHA, "x: y (gr7, td8)", base=["cli"], run=run, timeout=1.0)
    assert line == (
        f"land-notify {SHA[:9]}: gr7 auto_verify · failed: td8 (exit 3: "
        "[error:BadInput] nope)"
    )
    # The todo's failed put stops its tag: no STATUS:done without the note.
    assert [c[2] for c in seen] == ["put", "put"]
    assert all(c[0] == "cli" for c in seen)


def test_notify_survives_a_timeout(mod: ModuleType) -> None:
    def run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout)

    line = mod.notify(SHA, "x: y (gr7)", base=["cli"], run=run, timeout=1.0)
    assert line.endswith("failed: gr7 (TimeoutExpired)")


def _fake_cli(tmp_path: Path, rc: int) -> tuple[str, Path]:
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake_cli.py"
    fake.write_text(
        "import json, sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {fake}", log


def _main(cli: str, subject: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PRECIS_LAND_NOTIFY_CLI": cli}
    env.pop("PRECIS_LAND_NOTIFY", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sha", SHA, "--subject", subject],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )


def test_main_posts_through_the_cli(tmp_path: Path) -> None:
    cli, log = _fake_cli(tmp_path, 0)
    out = _main(cli, "feat(x): y (gr7, td8)")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"land-notify {SHA[:9]}: gr7 auto_verify · td8 done"
    lines = log.read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in lines]
    assert [c[:2] + c[3:4] + c[5:6] for c in calls] == [
        ["tools", "put", "gripe", "7"],
        ["tools", "put", "todo", "8"],
        ["tools", "tag", "todo", "8"],
    ]


def test_main_refusal_never_fails(tmp_path: Path) -> None:
    cli, _ = _fake_cli(tmp_path, 3)
    out = _main(cli, "feat(x): y (gr7)")
    assert out.returncode == 0
    assert "failed: gr7 (exit 3" in out.stdout


def test_main_without_handles_is_silent(tmp_path: Path) -> None:
    cli, log = _fake_cli(tmp_path, 0)
    out = _main(cli, "feat(x): y")
    assert out.returncode == 0 and out.stdout == ""
    assert not log.exists()
